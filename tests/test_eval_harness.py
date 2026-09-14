"""Eval harness: does scoring measure the environment, and only the agent's part?

The model is scripted here. What is under test is the harness and the attribution,
not a policy -- and the attribution claim is the one that matters, because if
anything other than the agent can move a score then every number the system
produces is suspect.
"""

from __future__ import annotations

import itertools
import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from tools.brokers import ActionBroker
from tools.brokers.env_broker import AGENT_WRITER, _FormParser
from tools.compose_fastapi_sqlite_v1 import WRITER_COLUMN, WRITER_HEADER, compose_app
from tools.eval_harness import EvalQuestion, GoldState, SiteEnvFactory, build_task
from tools.eval_harness.scorer import count_agent_rows

EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"
COMPETENT = (
    '{"kind": "goto", "selector": "/thread/3"}',
    '{"kind": "type", "selector": "#reply-body", "text": "I agree"}',
    '{"kind": "submit", "selector": "#reply-form"}',
    '{"kind": "stop"}',
)


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


class StepKeyedModel:
    """Answers by episode step, so it plays the same script in every episode."""

    model_id = "step-keyed"
    model_dir = Path("step-keyed")

    def generate(self, messages, limits):
        step = int(re.search(r"STEP: (\d+)", messages[-1]["content"]).group(1))
        return COMPETENT[min(step, len(COMPETENT) - 1)]


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
    gold=GoldState(table="replies", where={"thread_id": 3}, min_rows=1),
    max_steps=6,
)
_episode_ids = itertools.count()


def run_episode(factory, *completions):
    """Drive the loop directly. Returns the broker, the env, and the episode DB."""
    from tools.brokers import observation_from

    broker = ActionBroker(ScriptedModel(*completions))
    with factory.episode(QUESTION, f"ep{next(_episode_ids)}") as episode:
        env = episode.env
        view = env.observe()
        for step in range(QUESTION.max_steps):
            action = broker.act(observation_from(view, QUESTION.task, step, broker.counters))
            if action.kind.value in ("answer", "stop"):
                break
            view = env.apply(action, view.elements)
    return broker, env, str(episode.db_path)


def insert(db: str, writer: str | None) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        f"INSERT INTO replies (thread_id, body, author, created_at, {WRITER_COLUMN})"
        " VALUES (3, 'x', 'x', '2026-09-14 00:00:00', ?)",
        (writer,),
    )
    conn.commit()
    conn.close()


class TestEpisode:
    def test_a_competent_agent_scores_correct(self, factory):
        """The positive control. Without it, an always-fail scorer looks fine."""
        broker, _, db = run_episode(factory, *COMPETENT)
        assert count_agent_rows(db, QUESTION.gold) == 1, "a correct action sequence did not register"
        assert broker.counters.unparseable == 0

    def test_an_idle_agent_scores_incorrect(self, factory):
        _, _, db = run_episode(factory, '{"kind": "stop"}')
        assert count_agent_rows(db, QUESTION.gold) == 0

    def test_submitting_without_typing_does_not_count(self, factory):
        """The form requires a body; a bare submit must not create a row."""
        _, _, db = run_episode(
            factory,
            '{"kind": "goto", "selector": "/thread/3"}',
            '{"kind": "submit", "selector": "#reply-form"}',
            '{"kind": "stop"}',
        )
        assert count_agent_rows(db, QUESTION.gold) == 0

    def test_question_for_another_site_is_refused(self, factory):
        other = EvalQuestion(id="q", task="t", site_id="site-999999", gold=QUESTION.gold)
        with pytest.raises(AssertionError, match="site-999999"):
            with factory.episode(other, "x"):
                pass


class TestAttribution:
    """synthetic-population-spec §6: animation changes what the agent sees, never
    what it is judged on. Credit is positive: a row counts because it says `agent`,
    never because it failed to say anything else."""

    def test_seed_rows_are_not_credited_to_the_agent(self, factory):
        _, _, db = run_episode(factory, '{"kind": "stop"}')
        seeded = sqlite3.connect(db).execute(
            "SELECT COUNT(*) FROM replies WHERE thread_id = 3"
        ).fetchone()[0]
        assert seeded > 0, "thread 3 has no seed replies; test proves nothing"
        assert count_agent_rows(db, QUESTION.gold) == 0

    def test_driver_rows_are_excluded(self, factory):
        _, _, db = run_episode(factory, '{"kind": "stop"}')
        insert(db, "c_regulars")
        assert count_agent_rows(db, QUESTION.gold) == 0, "population activity moved the score"

    def test_unattributed_rows_are_excluded(self, factory):
        """The fail-closed direction: an anonymous write is nobody's, not the agent's."""
        _, _, db = run_episode(factory, '{"kind": "stop"}')
        insert(db, None)
        assert count_agent_rows(db, QUESTION.gold) == 0

    def test_agent_rows_are_credited(self, factory):
        """The other direction: exclusion must not swallow real agent writes."""
        _, _, db = run_episode(factory, '{"kind": "stop"}')
        insert(db, "agent")
        assert count_agent_rows(db, QUESTION.gold) == 1

    def test_site_refuses_an_unattributed_write(self, factory):
        """The site, not just the scorer, holds the line: no writer, no row."""
        from fastapi.testclient import TestClient

        _, _, db = run_episode(factory, '{"kind": "stop"}')
        site = compose_app(factory.spec, factory.content_dir, Path(db))
        with TestClient(site.app, base_url=f"http://{site.hostname}") as client:
            before = sqlite3.connect(db).execute("SELECT COUNT(*) FROM replies").fetchone()[0]
            anonymous = client.post("/thread/3/reply", data={"body": "hi"}, follow_redirects=False)
            forged = client.post(
                "/thread/3/reply", data={"body": "hi"},
                headers={WRITER_HEADER: "DROP TABLE"}, follow_redirects=False,
            )
            after = sqlite3.connect(db).execute("SELECT COUNT(*) FROM replies").fetchone()[0]
        assert anonymous.status_code == 400
        assert forged.status_code == 400
        assert after == before

    def test_env_broker_writes_as_agent(self):
        assert AGENT_WRITER == {WRITER_HEADER: "agent"}

    def test_scorer_refuses_unsafe_identifiers(self):
        with pytest.raises(AssertionError, match="unsafe table"):
            count_agent_rows(":memory:", GoldState(table="replies; DROP TABLE replies"))

    def test_scorer_refuses_a_table_it_cannot_attribute(self, tmp_path):
        db = tmp_path / "bare.sqlite"
        sqlite3.connect(db).execute("CREATE TABLE replies (id INTEGER PRIMARY KEY)").close()
        with pytest.raises(AssertionError, match="cannot attribute"):
            count_agent_rows(str(db), GoldState(table="replies"))


class TestPerEpisodeReset:
    def test_each_episode_starts_from_the_seed(self, factory):
        """Episodes must be comparable; a persistent DB silently breaks that."""
        _, _, first = run_episode(factory, *COMPETENT)
        _, _, second = run_episode(factory, '{"kind": "stop"}')
        assert first != second
        assert count_agent_rows(first, QUESTION.gold) == 1
        assert count_agent_rows(second, QUESTION.gold) == 0, "state leaked across episodes"

    def test_an_episode_id_cannot_be_reused(self, factory):
        with factory.episode(QUESTION, "same"):
            pass
        with pytest.raises(AssertionError, match="already ran"):
            with factory.episode(QUESTION, "same"):
                pass


class TestThroughInspect:
    """The wrapper is not free of behaviour: Inspect runs epochs concurrently, and
    the first version of this harness keyed episode databases by question, so epoch
    two reset epoch one's site before its scorer read it."""

    def test_every_epoch_of_a_competent_agent_scores_correct(self, factory, tmp_path):
        from inspect_ai import eval as inspect_eval

        broker = ActionBroker(StepKeyedModel())
        log = inspect_eval(
            build_task([QUESTION], broker, factory),
            model="mockllm/model",
            log_dir=str(tmp_path / "log"),
            display="none",
            epochs=3,
        )[0]
        assert log.status == "success", log.error
        assert len(log.samples) == 3
        for sample in log.samples:
            score = sample.scores["state_diff_scorer"]
            assert score.value == "C", (sample.epoch, score.explanation)
            assert sample.metadata["steps"] == 4
            assert sample.metadata["broker_counters"]["broker.requests"] == 4, (
                "per-sample counters should be the episode's delta, not the total"
            )

    def test_answer_questions_need_an_exact_answer(self, factory, tmp_path):
        from inspect_ai import eval as inspect_eval

        question = EvalQuestion(
            id="q_answer", task="What is 3?", site_id="site-000001", expected_answer="3"
        )
        broker = ActionBroker(ScriptedModel('{"kind": "answer", "text": "13"}'))
        log = inspect_eval(
            build_task([question], broker, factory),
            model="mockllm/model", log_dir=str(tmp_path / "log"), display="none",
        )[0]
        assert log.status == "success", log.error
        assert log.samples[0].scores["state_diff_scorer"].value == "I"


class TestEnvBrokerConfinement:
    def test_offsite_goto_is_refused_and_counted(self, factory):
        _, env, _ = run_episode(factory, '{"kind": "goto", "selector": "https://evil.example/"}')
        assert env.counters.off_site >= 1
        assert env.current_path == "/"

    def test_click_on_an_unlisted_selector_is_refused(self, factory):
        _, env, _ = run_episode(factory, '{"kind": "click", "selector": "#not-on-this-page"}')
        assert env.counters.denied >= 1

    def test_form_fields_are_named_the_way_the_page_parser_names_them(self):
        """A field with only a data-* attribute is offered to the agent under that
        selector, so the submit path must look its value up under the same one."""
        parser = _FormParser("#f")
        parser.feed(
            '<form id="f" action="/post"><input data-field="who" name="author">'
            '<textarea id="body-box" name="body"></textarea><input name="anon"></form>'
        )
        assert parser.action == "/post"
        assert parser.fields == [
            ('[data-field="who"]', "author"),
            ("#body-box", "body"),
            ("", "anon"),
        ]
