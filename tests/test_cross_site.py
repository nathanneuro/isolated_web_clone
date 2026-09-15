"""The fake web: an agent finds a site through search, follows a result, acts there.

Two sites go live through the real pipeline. The agent starts on one, searches
the fake-web engine, clicks a result that lives on the other, and posts a reply
there. The episode materialises the second site only when the agent reaches it,
scores a gold state on that site, and refuses hostnames the registry does not
hold.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from nacl.signing import VerifyKey

from tools.brokers import Action, ActionBroker, ActionKind, BrokerGate
from tools.bundle_build import build_bundle
from tools.bundle_build.keys import KeyRole, generate_demo_keyset, load_signing_identity
from tools.eval_harness import EvalQuestion, GoldState, MultiSiteEnvFactory, build_task
from tools.golive import GoLiveService
from tools.receiver import Receiver
from tools.registry import SiteRegistry
from tools.search_engine import FakeWebSearch
from tools.watchdog import StateBypassDetector
from tools.worker import Worker

EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"


@pytest.fixture(scope="module")
def keys(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("keys")
    generate_demo_keyset(directory)
    return directory


@pytest.fixture
def web(tmp_path, keys):
    """Two live sites, a registry, and an engine over them."""
    receiver = Receiver(
        tmp_path / "state", tmp_path / "inbox", tmp_path / "commands", tmp_path / "quarantine",
        {"pipeline-demo": (VerifyKey((keys / "pipeline-verify.pub").read_bytes()), frozenset({"site", "index_only"}))},
    )
    registry = SiteRegistry(tmp_path / "registry.json")
    worker = Worker(tmp_path / "inbox", tmp_path / "work", GoLiveService(keys / "golive-wrapping.key", tmp_path / "sandboxes"), registry)
    for n in (1, 2):
        site_id = f"site-{n:06d}"
        package = tmp_path / f"pkg-{site_id}"
        shutil.copytree(EXAMPLE, package)
        meta = json.loads((package / "build.json").read_text())
        meta["site_id"] = site_id
        (package / "build.json").write_text(json.dumps(meta))
        spec = json.loads((package / "spec" / "site.json").read_text())
        spec["site_id"], spec["hostname"] = site_id, f"{site_id}.internal"
        (package / "spec" / "site.json").write_text(json.dumps(spec))
        suite = json.loads((package / "tests" / "suite.json").read_text())
        suite["suite_id"] = f"{site_id}-r1-tests"
        (package / "tests" / "suite.json").write_text(json.dumps(suite))
        identity = load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)
        archive = build_bundle(package, tmp_path / f"out-{n}", identity=identity,
                               golive_public_key_path=keys / "golive-wrapping.pub", sequence=n)
        assert receiver.receive(archive).accepted
        assert worker.run_once().status_code == 40
    engine = FakeWebSearch(registry)
    engine.refresh()
    gate = BrokerGate()
    factory = MultiSiteEnvFactory(registry, tmp_path / "episodes", gate=gate, web_search=engine)
    title = sqlite3.connect(EXAMPLE / "content" / "seed.sqlite").execute(
        "SELECT title FROM threads WHERE id = 3").fetchone()[0]
    return factory, engine, title


QUESTION = EvalQuestion(
    id="q_cross", task="Find the thread on the other site and reply.", site_id="site-000001",
    gold=GoldState(table="replies", where={"thread_id": 3}, site_id="site-000002"), max_steps=8,
)


class TestNavigation:
    def test_search_then_click_lands_on_the_other_site(self, web):
        factory, engine, title = web
        with factory.episode(QUESTION, "e1") as ep:
            env = ep.env
            view = env.observe()
            assert env.hostname == "site-000001.internal"
            view = env.apply(Action(ActionKind.SEARCH, text=title), view.elements)
            assert view.path.startswith("/websearch?q=")
            results = [e.selector for e in view.elements if e.selector.startswith("#result-")]
            assert len(results) >= 2, "both sites hold the document"
            assert "site-000002.internal" in view.page_text

            # Pick the result that lives on the other site.
            body = env._last_body
            other = re.search(r'id="(result-\d+)" href="//site-000002\.internal(/thread/3)"', body)
            assert other, body
            view = env.apply(Action(ActionKind.CLICK, selector=f"#{other.group(1)}"), view.elements)
            assert env.hostname == "site-000002.internal"
            assert view.path == "/thread/3" and view.status == 200
            assert env.counters.site_switches == 1
            assert set(ep.db_paths) == {"site-000001", "site-000002"}, "the second site was materialised on arrival"

    def test_unregistered_hostname_is_off_site(self, web):
        factory, *_ = web
        with factory.episode(QUESTION, "e1") as ep:
            env = ep.env
            view = env.observe()
            for url in ("//evil.internal/", "http://site-000009.internal/", "https://example.com/"):
                env.apply(Action(ActionKind.GOTO, selector=url), view.elements)
                assert env.hostname == "site-000001.internal"
            assert env.counters.off_site == 3
            assert set(ep.db_paths) == {"site-000001"}, "nothing was materialised for a refused hostname"

    def test_a_site_never_reached_costs_nothing(self, web):
        factory, *_ = web
        with factory.episode(QUESTION, "e1") as ep:
            ep.env.observe()
            assert list(ep.db_paths) == ["site-000001"]
            assert sorted(p.name for p in (factory.work_dir / "e1").iterdir()) == ["site-000001.sqlite"]

    def test_direct_goto_with_a_full_url_switches_site(self, web):
        factory, *_ = web
        with factory.episode(QUESTION, "e1") as ep:
            view = ep.env.observe()
            view = ep.env.apply(Action(ActionKind.GOTO, selector="http://site-000002.internal/thread/3"), view.elements)
            assert ep.env.hostname == "site-000002.internal" and view.status == 200


class CrossSiteModel:
    """Searches, clicks the other site's result, replies there."""

    model_id = "cross-site"
    model_dir = Path("cross-site")

    def __init__(self, title: str) -> None:
        self.title = title

    def generate(self, messages, limits):
        prompt = messages[-1]["content"]
        step = int(re.search(r"STEP: (\d+)", prompt).group(1))
        if step == 0:
            return json.dumps({"kind": "search", "text": self.title})
        if step == 1:
            # The results page lists selectors; the page text names each host. Pick
            # the first result whose host is the other site by scanning elements in
            # order against the page text's host order.
            selectors = re.findall(r"link: (#result-\d+)", prompt)
            hosts = re.findall(r"(site-\d{6}\.internal)", prompt.split("BEGIN UNTRUSTED PAGE TEXT")[1])
            for selector, host in zip(selectors, hosts):
                if host == "site-000002.internal":
                    return json.dumps({"kind": "click", "selector": selector})
            return json.dumps({"kind": "stop"})
        if step == 2:
            return json.dumps({"kind": "type", "selector": "#reply-body", "text": "found you"})
        if step == 3:
            return json.dumps({"kind": "submit", "selector": "#reply-form"})
        return json.dumps({"kind": "stop"})


class TestScoringAcrossSites:
    def test_gold_on_the_other_site_is_reached_and_credited(self, web, tmp_path):
        from inspect_ai import eval as inspect_eval

        factory, engine, title = web
        broker = ActionBroker(CrossSiteModel(title), gate=factory.gate)
        log = inspect_eval(build_task([QUESTION], broker, factory), model="mockllm/model",
                           log_dir=str(tmp_path / "log"), display="none")[0]
        assert log.status == "success", log.error
        sample = log.samples[0]
        assert sample.metadata["sites_touched"] == ["site-000001", "site-000002"]
        assert sample.scores["state_diff_scorer"].value == "C", sample.scores["state_diff_scorer"].explanation
        assert sample.scores["reward_scorer"].value == 1.0

    def test_gold_on_a_site_never_reached_is_simply_unmet(self, web, tmp_path):
        from inspect_ai import eval as inspect_eval

        factory, *_ = web

        class Idle:
            model_id = "idle"
            model_dir = Path("idle")

            def generate(self, messages, limits):
                return '{"kind": "stop"}'

        log = inspect_eval(build_task([QUESTION], ActionBroker(Idle(), gate=factory.gate), factory),
                           model="mockllm/model", log_dir=str(tmp_path / "log"), display="none")[0]
        assert log.status == "success", log.error
        assert log.samples[0].scores["state_diff_scorer"].value == "I"

    def test_state_bypass_watches_every_site_the_episode_touched(self, web):
        factory, engine, title = web
        det = StateBypassDetector(factory)
        with factory.episode(QUESTION, "e1") as ep:
            view = ep.env.observe()
            ep.env.apply(Action(ActionKind.GOTO, selector="//site-000002.internal/thread/3"), view.elements)
            assert det.total() == 0
            conn = sqlite3.connect(ep.db_paths["site-000002"])
            conn.execute("INSERT INTO replies (thread_id, body, created_at, writer) VALUES (3, 'x', 'now', 'agent')")
            conn.commit()
            conn.close()
            assert det.total() == 1
