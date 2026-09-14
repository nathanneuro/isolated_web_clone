"""Eval harness: does scoring measure the environment, and only the agent's part?

The model is scripted here. What is under test is the harness and the attribution,
not a policy -- and the attribution claim is the one that matters, because if
population activity can move a score then every number the system produces is
suspect.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from tools.brokers import ActionBroker
from tools.eval_harness import EvalQuestion, GoldState, SiteEnvFactory
from tools.eval_harness.scorer import count_agent_rows

EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"


class ScriptedModel:
    """Emits a fixed sequence of completions, then stops."""

    def __init__(self, *completions: str) -> None:
        self.script = list(completions)
        self.model_dir = Path("scripted")

    @property
    def model_id(self) -> str:
        return "scripted"

    def generate(self, messages, limits):
        return self.script.pop(0) if self.script else '{"kind": "stop"}'


@pytest.fixture
def factory(tmp_path):
    work = tmp_path / "content"
    shutil.copytree(EXAMPLE / "content", work / "content")
    shutil.copytree(EXAMPLE / "index", work / "index")
    spec = json.loads((EXAMPLE / "spec" / "site.json").read_text())
    return SiteEnvFactory(spec, work, work / "content" / "seed.sqlite", tmp_path / "eps")


QUESTION = EvalQuestion(
    id="q_reply",
    task="Open thread 3 and reply.",
    site_id="site-000001",
    gold=GoldState(table="replies", where={"thread_id": "3"}, min_rows=1),
    max_steps=6,
)


def run_episode(factory, *completions):
    """Drive the loop directly; the Inspect wrapper adds nothing under test."""
    from tools.brokers import observation_from

    broker = ActionBroker(ScriptedModel(*completions))
    env = factory(QUESTION)
    view = env.observe()
    for step in range(QUESTION.max_steps):
        action = broker.act(observation_from(view, QUESTION.task, step, broker.counters))
        if action.kind.value in ("answer", "stop"):
            break
        view = env.apply(action, view.elements)
    return broker, env


class TestEpisode:
    def test_a_competent_agent_scores_correct(self, factory):
        """The positive control. Without it, an always-fail scorer looks fine."""
        broker, _ = run_episode(
            factory,
            '{"kind": "goto", "selector": "/thread/3"}',
            '{"kind": "type", "selector": "#reply-body", "text": "I agree"}',
            '{"kind": "submit", "selector": "#reply-form"}',
            '{"kind": "stop"}',
        )
        rows = count_agent_rows(str(factory.db_path_for(QUESTION)), QUESTION.gold)
        assert rows >= 1, "a correct action sequence did not register"
        assert broker.counters.unparseable == 0

    def test_an_idle_agent_scores_incorrect(self, factory):
        run_episode(factory, '{"kind": "stop"}')
        assert count_agent_rows(str(factory.db_path_for(QUESTION)), QUESTION.gold) == 0

    def test_submitting_without_typing_does_not_count(self, factory):
        """The form requires a body; a bare submit must not create a row."""
        run_episode(
            factory,
            '{"kind": "goto", "selector": "/thread/3"}',
            '{"kind": "submit", "selector": "#reply-form"}',
            '{"kind": "stop"}',
        )
        assert count_agent_rows(str(factory.db_path_for(QUESTION)), QUESTION.gold) == 0


class TestAttribution:
    """synthetic-population-spec §6: animation changes what the agent sees, never
    what it is judged on."""

    def test_seed_rows_are_not_credited_to_the_agent(self, factory):
        run_episode(factory, '{"kind": "stop"}')
        db = factory.db_path_for(QUESTION)
        seeded = sqlite3.connect(db).execute(
            "SELECT COUNT(*) FROM replies WHERE thread_id = 3"
        ).fetchone()[0]
        assert count_agent_rows(str(db), QUESTION.gold) == 0
        assert seeded >= 0  # whatever the seed holds, none of it is the agent's

    def test_driver_rows_are_excluded(self, factory):
        """The load-bearing test. Inject a driver-tagged row and score again."""
        run_episode(factory, '{"kind": "stop"}')
        db = str(factory.db_path_for(QUESTION))
        before = count_agent_rows(db, QUESTION.gold)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO replies (thread_id, body, author, created_at, driver_tag)"
            " VALUES (3, 'bot reply', 'bot', '2026-09-14 00:00:00', 'c_regulars')"
        )
        conn.commit()
        conn.close()
        assert count_agent_rows(db, QUESTION.gold) == before, (
            "population activity moved the score"
        )

    def test_an_untagged_row_is_credited(self, factory):
        """The other direction: exclusion must not swallow real agent writes."""
        run_episode(factory, '{"kind": "stop"}')
        db = str(factory.db_path_for(QUESTION))
        before = count_agent_rows(db, QUESTION.gold)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO replies (thread_id, body, author, created_at, driver_tag)"
            " VALUES (3, 'agent reply', 'agent', '2026-09-14 00:00:00', NULL)"
        )
        conn.commit()
        conn.close()
        assert count_agent_rows(db, QUESTION.gold) == before + 1

    def test_scorer_refuses_unsafe_identifiers(self):
        with pytest.raises(AssertionError, match="unsafe table"):
            count_agent_rows(":memory:", GoldState(table="replies; DROP TABLE replies"))


class TestPerEpisodeReset:
    def test_each_episode_starts_from_the_seed(self, factory):
        """Episodes must be comparable; a persistent DB silently breaks that."""
        run_episode(
            factory,
            '{"kind": "goto", "selector": "/thread/3"}',
            '{"kind": "type", "selector": "#reply-body", "text": "first"}',
            '{"kind": "submit", "selector": "#reply-form"}',
            '{"kind": "stop"}',
        )
        db = str(factory.db_path_for(QUESTION))
        assert count_agent_rows(db, QUESTION.gold) == 1
        run_episode(factory, '{"kind": "stop"}')
        assert count_agent_rows(db, QUESTION.gold) == 0, "state leaked across episodes"


class TestEnvBrokerConfinement:
    def test_offsite_goto_is_refused_and_counted(self, factory):
        _, env = run_episode(factory, '{"kind": "goto", "selector": "https://evil.example/"}')
        assert env.counters.off_site >= 1
        assert env.current_path == "/"

    def test_click_on_an_unlisted_selector_is_refused(self, factory):
        _, env = run_episode(factory, '{"kind": "click", "selector": "#not-on-this-page"}')
        assert env.counters.denied >= 1
