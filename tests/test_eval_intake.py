"""Eval choreography through the diode: dev-signed, unsealed inside, checked
against the live site, and fired on its step during an episode."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from nacl.signing import VerifyKey

from tools.brokers import BrokerGate
from tools.bundle_build import BuildError, build_bundle, build_eval_bundle
from tools.bundle_build.keys import KeyRole, generate_demo_keyset, load_signing_identity
from tools.eval_harness import EvalQuestion, GoldState, MultiSiteEnvFactory
from tools.eval_harness.scorer import count_agent_rows
from tools.eval_intake import EvalDefinitions, EvalIntake, IntakeStatus
from tools.golive import GoLiveService
from tools.receiver import Receiver, Status
from tools.registry import SiteRegistry
from tools.worker import Worker

EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"

CHOREOGRAPHY = {
    "choreography_id": "eval-demo-site-000001",
    "site_id": "site-000001",
    "question_id": "q_race",
    "actors": [
        {"id": "a_announcer", "user_ref": "u_00043117", "script": [
            {"at_step": 2, "action": "form_submit", "form": "f_reply", "route": "r_reply",
             "content_pool": "cp_announce", "pool_row": 0},
        ]},
    ],
    "ambient": "suppress_for_actors",
    "content_pools": [{"id": "cp_announce", "blob_ref": "content/pools/announce.json", "row_count": 1}],
}


@pytest.fixture
def inside(tmp_path):
    keys = tmp_path / "keys"
    generate_demo_keyset(keys)
    receiver = Receiver(
        tmp_path / "state", tmp_path / "inbox", tmp_path / "commands", tmp_path / "quarantine",
        {
            "pipeline-demo": (VerifyKey((keys / "pipeline-verify.pub").read_bytes()), frozenset({"site", "index_only"})),
            "dev-demo": (VerifyKey((keys / "dev-verify.pub").read_bytes()), frozenset({"command", "eval"})),
        },
        eval_inbox=tmp_path / "eval-inbox",
    )
    golive = GoLiveService(keys / "golive-wrapping.key", tmp_path / "sandboxes")
    registry = SiteRegistry(tmp_path / "registry.json")
    worker = Worker(tmp_path / "inbox", tmp_path / "work", golive, registry)
    definitions = EvalDefinitions(tmp_path / "eval-definitions.json")
    intake = EvalIntake(tmp_path / "eval-inbox", golive, registry, definitions)
    return keys, receiver, worker, registry, intake, definitions


def site_live(tmp_path, keys, receiver, worker) -> None:
    identity = load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)
    archive = build_bundle(EXAMPLE, tmp_path / "out-site", identity=identity,
                           golive_public_key_path=keys / "golive-wrapping.pub", sequence=1)
    assert receiver.receive(archive).accepted
    assert worker.run_once().status_code == 40


def eval_package(tmp_path, doc=CHOREOGRAPHY, *, revision=1) -> Path:
    package = tmp_path / f"eval-pkg-r{revision}"
    (package / "spec").mkdir(parents=True)
    (package / "content" / "pools").mkdir(parents=True)
    (package / "build.json").write_text(json.dumps({
        "eval_id": "demo-q-race", "revision": revision, "created_at": "2026-09-14T18:40:00Z", "golive_key_id": "golive-demo",
    }))
    (package / "spec" / "choreography.json").write_text(json.dumps(doc))
    (package / "content" / "pools" / "announce.json").write_text(json.dumps([{"body": "the announcement", "author": "announcer"}]))
    return package


def ship_eval(tmp_path, keys, receiver, package, *, role=KeyRole.DEV, sequence=10):
    key_id, path = ("dev-demo", "dev-signing.key") if role is KeyRole.DEV else ("pipeline-demo", "pipeline-signing.key")
    identity = load_signing_identity(keys / path, key_id, role)
    archive = build_eval_bundle(package, tmp_path / f"out-eval-{sequence}", identity=identity,
                                golive_public_key_path=keys / "golive-wrapping.pub", sequence=sequence)
    return receiver.receive(archive)


class TestIntake:
    def test_choreography_is_unsealed_filed_and_fired(self, tmp_path, inside):
        keys, receiver, worker, registry, intake, definitions = inside
        site_live(tmp_path, keys, receiver, worker)
        receipt = ship_eval(tmp_path, keys, receiver, eval_package(tmp_path))
        assert receipt.status is Status.OK, receipt.detail
        assert intake.run_once().status_code is IntakeStatus.FILED
        sandbox = definitions.sandbox_for("q_race")
        assert (sandbox / "spec" / "choreography.json").is_file()
        pool = json.loads((sandbox / "spec" / "choreography.json").read_text())["content_pools"][0]["blob_ref"]
        assert json.loads((sandbox / pool).read_text())[0]["body"] == "the announcement"

        factory = MultiSiteEnvFactory(registry, tmp_path / "eps", gate=BrokerGate(), run_id="r1", definitions=definitions)
        question = EvalQuestion(id="q_race", task="reply before the announcer", site_id="site-000001",
                                gold=GoldState(table="replies", where={"thread_id": 3}), max_steps=6)
        with factory.episode(question, "e1") as ep:
            ep.env.observe()
            driver = ep.drivers["site-000001"]
            assert driver.choreography is not None and driver.population is not None
            for step in range(4):
                ep.tick(step)
            rows = sqlite3.connect(ep.db_path).execute(
                "SELECT body, writer FROM replies WHERE writer = 'a_announcer'").fetchall()
            assert rows == [("the announcement", "a_announcer")]
            fired = [a for a in driver.performed if a.actor == "a_announcer"]
            assert [a.step for a in fired] == [2], "the announcer fires on its step and only then"
            assert count_agent_rows(str(ep.db_path), question.gold) == 0

    def test_another_question_does_not_get_the_choreography(self, tmp_path, inside):
        keys, receiver, worker, registry, intake, definitions = inside
        site_live(tmp_path, keys, receiver, worker)
        ship_eval(tmp_path, keys, receiver, eval_package(tmp_path))
        intake.run_once()
        factory = MultiSiteEnvFactory(registry, tmp_path / "eps", gate=BrokerGate(), definitions=definitions)
        other = EvalQuestion(id="q_other", task="t", site_id="site-000001", gold=GoldState(table="replies"))
        with factory.episode(other, "e1") as ep:
            assert ep.drivers["site-000001"].choreography is None

    def test_pipeline_key_cannot_sign_an_eval_bundle(self, tmp_path, inside):
        keys, receiver, *_ = inside
        with pytest.raises(BuildError, match="dev key"):
            ship_eval(tmp_path, keys, receiver, eval_package(tmp_path), role=KeyRole.PIPELINE)

    def test_a_dev_key_not_allowed_for_eval_is_rejected_at_the_receiver(self, tmp_path, inside):
        keys, receiver, *_ = inside
        receiver.verify_keys["dev-demo"] = (receiver.verify_keys["dev-demo"][0], frozenset({"command"}))
        receipt = ship_eval(tmp_path, keys, receiver, eval_package(tmp_path))
        assert receipt.status is Status.REJECT_SIGNATURE

    def test_choreography_for_a_site_that_is_not_live_waits(self, tmp_path, inside):
        keys, receiver, worker, registry, intake, definitions = inside
        assert ship_eval(tmp_path, keys, receiver, eval_package(tmp_path)).accepted
        assert intake.run_once().status_code is IntakeStatus.SITE_NOT_LIVE
        assert len(definitions) == 0

    def test_choreography_naming_a_form_the_live_site_lacks_is_unsupported(self, tmp_path, inside):
        keys, receiver, worker, registry, intake, definitions = inside
        site_live(tmp_path, keys, receiver, worker)
        doc = json.loads(json.dumps(CHOREOGRAPHY))
        doc["actors"][0]["script"][0]["form"] = "f_new_thread"
        doc["actors"][0]["script"][0]["route"] = "r_new_thread"
        assert ship_eval(tmp_path, keys, receiver, eval_package(tmp_path, doc)).accepted, "the bundle alone cannot know"
        assert intake.run_once().status_code is IntakeStatus.UNSUPPORTED
        assert len(definitions) == 0

    def test_free_text_in_a_choreography_is_refused_at_build(self, tmp_path, inside):
        keys, receiver, *_ = inside
        doc = json.loads(json.dumps(CHOREOGRAPHY))
        doc["actors"][0]["script"][0]["note"] = "post the announcement, then wait"
        with pytest.raises(BuildError, match="LINT-SCHEMA"):
            ship_eval(tmp_path, keys, receiver, eval_package(tmp_path, doc))

    def test_a_newer_revision_replaces_the_filed_one(self, tmp_path, inside):
        keys, receiver, worker, registry, intake, definitions = inside
        site_live(tmp_path, keys, receiver, worker)
        ship_eval(tmp_path, keys, receiver, eval_package(tmp_path), sequence=10)
        intake.run_once()
        first = definitions.sandbox_for("q_race")
        doc = json.loads(json.dumps(CHOREOGRAPHY))
        doc["actors"][0]["script"][0]["at_step"] = 4
        ship_eval(tmp_path, keys, receiver, eval_package(tmp_path, doc, revision=2), sequence=11)
        intake.run_once()
        second = definitions.sandbox_for("q_race")
        assert first != second and second.name == "eval-demo-q-race-r2"
