"""Population driver: deterministic, attributable, and budgeted.

The test that matters most is the end-to-end one at the bottom: with the driver
running, an idle agent's score must be exactly what it was with the driver off.
If animation can move a score, every number this system produces is suspect.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from tools.brokers import ActionBroker, observation_from
from tools.compose_fastapi_sqlite_v1 import compose_app
from tools.eval_harness import EvalQuestion, GoldState, SiteEnvFactory
from tools.eval_harness.scorer import count_agent_rows
from tools.population import Choreography, Population, PopulationDriver
from tools.population.driver import Actor, Behaviour, Cohort, ScriptStep

EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"

POOLS = {
    "cp_replies": [{"body": f"ambient reply {i}", "author": f"user{i}"} for i in range(20)],
    "cp_q7": [{"body": "the announcement", "author": "announcer"}],
}

AMBIENT = Population(
    population_id="site-000001-pop-r1",
    site_id="site-000001",
    cohorts=(
        Cohort("c_lurkers", user_count=400),
        Cohort(
            "c_regulars",
            user_count=60,
            behaviours=(
                Behaviour("b_reply", "form_submit", "f_reply", "r_reply", "cp_replies",
                          rate_per_hour=30.0),
            ),
        ),
    ),
)

CHOREO = Choreography(
    choreography_id="eval-demo-site-000001",
    site_id="site-000001",
    question_id="q_reply",
    actors=(
        Actor("a_announcer", "u_00043117",
              script=(ScriptStep(2, "form_submit", "f_reply", "r_reply", "cp_q7", 0),)),
    ),
)


@pytest.fixture
def site(tmp_path):
    work = tmp_path / "content"
    shutil.copytree(EXAMPLE / "content", work / "content")
    shutil.copytree(EXAMPLE / "index", work / "index")
    spec = json.loads((EXAMPLE / "spec" / "site.json").read_text())
    db = tmp_path / "episode.sqlite"
    shutil.copyfile(work / "content" / "seed.sqlite", db)
    composed = compose_app(spec, work, db)
    return spec, composed, db


def driver(site, *, population=AMBIENT, choreography=None, episode="e1", **kw):
    from fastapi.testclient import TestClient

    spec, composed, _ = site
    client = TestClient(composed.app, base_url=f"http://{composed.hostname}")
    client.__enter__()
    return PopulationDriver(
        client, spec, run_id="r1", episode_id=episode, site_id="site-000001",
        population=population, choreography=choreography, pools=POOLS, **kw,
    )


def run(d, steps=6):
    return [a for step in range(steps) for a in d.tick(step)]


class TestDeterminism:
    def test_same_seed_gives_the_same_activity(self, site, tmp_path):
        first = [(a.step, a.actor) for a in run(driver(site))]
        second_site = site
        second = [(a.step, a.actor) for a in run(driver(second_site, episode="e1"))]
        assert first == second

    def test_different_episode_gives_different_activity(self, site):
        first = [(a.step, a.actor, a.route) for a in run(driver(site, episode="e1"))]
        second = [(a.step, a.actor, a.route) for a in run(driver(site, episode="e2"))]
        assert first != second, "episodes are not independently seeded"

    def test_no_wall_clock_in_the_seed(self, site):
        """Two drivers built at different times with the same identity must agree."""
        import time

        first = [a.route for a in run(driver(site, episode="e9"))]
        time.sleep(0.01)
        second = [a.route for a in run(driver(site, episode="e9"))]
        assert first == second


class TestChoreography:
    def test_actor_fires_on_its_exact_step(self, site):
        d = driver(site, population=None, choreography=CHOREO)
        by_step = {step: d.tick(step) for step in range(6)}
        assert by_step[2] and by_step[2][0].actor == "a_announcer"
        assert all(not by_step[s] for s in (0, 1, 3, 4, 5))

    def test_suppress_for_actors_reports_the_actor_set(self, site):
        d = driver(site, choreography=CHOREO)
        assert d.suppressed_actors == frozenset({"u_00043117"})

    def test_suppress_all_stops_ambient(self, site):
        quiet = Choreography("c", "site-000001", "q", CHOREO.actors, ambient="suppress_all")
        d = driver(site, choreography=quiet)
        actors = {a.actor for a in run(d)}
        assert "c_regulars" not in actors
        assert d.counters.ambient_suppressed > 0

    def test_choreography_uses_the_declared_pool_row(self, site):
        _, _, db = site
        d = driver(site, population=None, choreography=CHOREO)
        run(d)
        rows = sqlite3.connect(db).execute(
            "SELECT body FROM replies WHERE writer = 'a_announcer'"
        ).fetchall()
        assert [r[0] for r in rows] == ["the announcement"]


class TestBudget:
    def test_absurd_rate_is_clamped_not_obeyed(self, site):
        """The spec is authored outside; outside is not trusted about inside cost."""
        greedy = Population(
            "p", "site-000001",
            cohorts=(Cohort("c_flood", 100000, (
                Behaviour("b", "form_submit", "f_reply", "r_reply", "cp_replies",
                          rate_per_hour=10000.0),
            )),),
        )
        d = driver(site, population=greedy, max_actions=5)
        run(d)
        assert d.counters.actions_performed == 5
        assert d.counters.actions_clamped > 0

    def test_unsupported_action_is_refused_at_construction(self):
        with pytest.raises(AssertionError, match="unsupported action"):
            Behaviour("b", "execute_script", "f", "r", "cp")


class TestConfinement:
    def test_driver_holds_no_database_handle(self, site):
        """Writes go through declared forms, so it can only do what a user could."""
        d = driver(site)
        held = {type(v).__name__ for v in vars(d).values()}
        assert not {"Connection", "Cursor"} & held

    def test_every_write_is_attributed_to_its_actor(self, site):
        _, _, db = site
        d = driver(site)
        run(d)
        writers = {
            row[0] for row in sqlite3.connect(db).execute(
                "SELECT DISTINCT writer FROM replies WHERE writer IS NOT NULL"
            )
        }
        assert d.counters.actions_performed > 0, "driver did nothing; test proves nothing"
        assert writers == {a.actor for a in d.performed}
        assert "agent" not in writers

    def test_counter_names_are_integers(self, site):
        d = driver(site)
        run(d)
        assert all(isinstance(v, int) for v in d.counters.as_metrics().values())


class TestScoreIsUnmoved:
    """synthetic-population-spec §6, end to end."""

    @pytest.fixture
    def factory(self, tmp_path):
        work = tmp_path / "c"
        shutil.copytree(EXAMPLE / "content", work / "content")
        shutil.copytree(EXAMPLE / "index", work / "index")
        spec = json.loads((EXAMPLE / "spec" / "site.json").read_text())
        return SiteEnvFactory(spec, work, work / "content" / "seed.sqlite", tmp_path / "eps")

    def test_animation_does_not_move_an_idle_agents_score(self, factory, tmp_path):
        question = EvalQuestion(
            id="q", task="do nothing", site_id="site-000001",
            gold=GoldState(table="replies", where={}, min_rows=1), max_steps=3,
        )
        spec = factory.spec

        def episode(with_driver: bool) -> int:
            from fastapi.testclient import TestClient

            with factory.episode(question, f"driver_{with_driver}") as ep:
                if with_driver:
                    composed = compose_app(spec, factory.content_dir, ep.db_path)
                    with TestClient(composed.app, base_url=f"http://{composed.hostname}") as client:
                        d = PopulationDriver(
                            client, spec, run_id="r", episode_id="e", site_id="site-000001",
                            population=AMBIENT, pools=POOLS,
                        )
                        for step in range(3):
                            d.tick(step)
                    assert d.counters.actions_performed > 0, "driver idle; test proves nothing"
                ep.env.observe()
            return count_agent_rows(str(ep.db_path), question.gold)

        assert episode(False) == episode(True) == 0


class TestThroughTheDiode:
    """The population ships with the site: linted at build and receipt, its pools
    encrypted, decrypted by go-live, and driven inside from the sandbox alone."""

    @pytest.fixture
    def inside(self, tmp_path):
        from nacl.signing import VerifyKey

        from tools.bundle_build import build_bundle
        from tools.bundle_build.keys import KeyRole, generate_demo_keyset, load_signing_identity
        from tools.golive import GoLiveService
        from tools.receiver import Receiver
        from tools.registry import SiteRegistry
        from tools.worker import Worker

        keys = tmp_path / "keys"
        generate_demo_keyset(keys)
        receiver = Receiver(
            tmp_path / "state", tmp_path / "inbox", tmp_path / "commands", tmp_path / "quarantine",
            {"pipeline-demo": (VerifyKey((keys / "pipeline-verify.pub").read_bytes()), frozenset({"site", "index_only"}))},
        )
        registry = SiteRegistry(tmp_path / "registry.json")
        worker = Worker(tmp_path / "inbox", tmp_path / "work", GoLiveService(keys / "golive-wrapping.key", tmp_path / "sb"), registry)

        def ship(mutate=None, sequence=1):
            package = tmp_path / f"pkg-{sequence}"
            shutil.copytree(EXAMPLE, package)
            if mutate:
                mutate(package)
            identity = load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)
            archive = build_bundle(package, tmp_path / f"out-{sequence}", identity=identity,
                                   golive_public_key_path=keys / "golive-wrapping.pub", sequence=sequence)
            return receiver.receive(archive)

        return ship, worker, registry

    def test_population_arrives_and_drives_from_the_sandbox(self, inside, tmp_path):
        ship, worker, registry = inside
        receipt = ship()
        assert receipt.accepted, receipt.detail
        assert worker.run_once().status_code == 40
        sandbox = Path(registry.live_sites()[0].deployment)
        assert (sandbox / "spec" / "population.json").is_file()
        pool_ref = json.loads((sandbox / "spec" / "population.json").read_text())["content_pools"][0]["blob_ref"]
        assert (sandbox / pool_ref).is_file(), "the pool was decrypted beside the site"
        ciphertext = next((tmp_path / "work").rglob(Path(pool_ref).name)).read_bytes()
        assert ciphertext != (sandbox / pool_ref).read_bytes(), "the worker's copy is ciphertext"

        from tools.brokers import BrokerGate
        from tools.eval_harness import EvalQuestion, GoldState, MultiSiteEnvFactory
        from tools.eval_harness.scorer import count_agent_rows

        factory = MultiSiteEnvFactory(registry, tmp_path / "eps", gate=BrokerGate(), run_id="r1")
        question = EvalQuestion(id="q", task="do nothing", site_id="site-000001", gold=GoldState(table="replies"), max_steps=3)
        with factory.episode(question, "e1") as ep:
            assert list(ep.drivers) == ["site-000001"]
            ep.env.observe()
            for step in range(6):
                ep.tick(step)
            driver = ep.drivers["site-000001"]
            assert driver.counters.actions_performed > 0, "the shipped population did nothing"
            writers = {r[0] for r in sqlite3.connect(ep.db_path).execute(
                "SELECT DISTINCT writer FROM replies WHERE writer IS NOT NULL")}
            assert writers == {"c_regulars"}
            assert count_agent_rows(str(ep.db_path), question.gold) == 0, "animation moved the score"

    def test_population_naming_an_unknown_form_is_rejected_at_build(self, inside):
        from tools.bundle_build import BuildError

        ship, *_ = inside

        def mutate(package: Path) -> None:
            doc = json.loads((package / "spec" / "population.json").read_text())
            doc["cohorts"][1]["behaviours"][0]["form"] = "f_missing"
            (package / "spec" / "population.json").write_text(json.dumps(doc))

        with pytest.raises(BuildError, match="LINT-XREF-02"):
            ship(mutate)

    def test_pool_with_the_wrong_row_count_is_refused_inside(self, inside, tmp_path):
        """The declared count is structure; the blob is content. They must agree."""
        ship, worker, registry = inside

        def mutate(package: Path) -> None:
            doc = json.loads((package / "spec" / "population.json").read_text())
            doc["content_pools"][0]["row_count"] = 7
            (package / "spec" / "population.json").write_text(json.dumps(doc))

        assert ship(mutate).accepted
        assert worker.run_once().status_code == 40
        from tools.population import PopulationDriver

        sandbox = Path(registry.live_sites()[0].deployment)
        with pytest.raises(AssertionError, match="declared 7 rows"):
            PopulationDriver.from_sandbox(sandbox, object(), run_id="r", episode_id="e")

    def test_a_site_without_a_population_is_still_a_site(self, inside):
        ship, worker, registry = inside

        def mutate(package: Path) -> None:
            (package / "spec" / "population.json").unlink()
            shutil.rmtree(package / "content" / "pools")

        assert ship(mutate).accepted
        assert worker.run_once().status_code == 40
        from tools.population import PopulationDriver

        assert PopulationDriver.from_sandbox(Path(registry.live_sites()[0].deployment), object(), run_id="r", episode_id="e") is None
