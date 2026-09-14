"""The inside worker: codes out, structure in, nothing improvised.

Each test drives the real path -- bundle-build outside, receiver inside, worker,
go-live, registry -- because the worker's contract is about what it does with the
codes it gets back, and a mocked go-live would let the test agree with itself.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from nacl.signing import VerifyKey

from tools.bundle_build import build_bundle
from tools.bundle_build.keys import KeyRole, generate_demo_keyset, load_signing_identity
from tools.compose_fastapi_sqlite_v1 import classify_spec
from tools.golive import GoLiveService
from tools.receiver import Receiver
from tools.registry import SiteRegistry, SiteStatus
from tools.worker import Worker, WorkerStatus
from tools.worker.worker import (
    EXTERNAL_REQUEST,
    MOUNT_INCONSISTENT,
    UNSUPPORTED_ELEMENT,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"


@pytest.fixture(scope="module")
def keys(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("keys")
    generate_demo_keyset(directory)
    return directory


@pytest.fixture
def inside(tmp_path, keys):
    receiver = Receiver(
        tmp_path / "state", tmp_path / "inbox", tmp_path / "commands", tmp_path / "quarantine",
        {"pipeline-demo": (VerifyKey((keys / "pipeline-verify.pub").read_bytes()), frozenset({"site", "index_only"}))},
    )
    golive = GoLiveService(keys / "golive-wrapping.key", tmp_path / "sandboxes")
    registry = SiteRegistry(tmp_path / "registry.json")
    worker = Worker(tmp_path / "inbox", tmp_path / "work", golive, registry)
    return receiver, worker, registry


def ship(tmp_path, keys, receiver, *, sequence: int, revision: int = 1, supersedes: str | None = None, mutate=None) -> str:
    """Build a bundle from the example package (optionally altered) and receive it."""
    package = tmp_path / f"package-r{revision}"
    shutil.copytree(EXAMPLE, package)
    meta = json.loads((package / "build.json").read_text())
    meta["revision"] = revision
    if supersedes:
        meta["supersedes"] = supersedes
    (package / "build.json").write_text(json.dumps(meta))
    suite = json.loads((package / "tests" / "suite.json").read_text())
    suite["suite_id"] = f"{meta['site_id']}-r{revision}-tests"
    (package / "tests" / "suite.json").write_text(json.dumps(suite))
    if mutate:
        mutate(package)
    identity = load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)
    archive = build_bundle(
        package, tmp_path / f"out-{sequence}", identity=identity,
        golive_public_key_path=keys / "golive-wrapping.pub", sequence=sequence,
    )
    receipt = receiver.receive(archive)
    assert receipt.accepted, receipt.detail
    return receipt.bundle_id


class TestHappyPath:
    def test_valid_bundle_goes_live_and_is_registered(self, tmp_path, keys, inside):
        receiver, worker, registry = inside
        bundle_id = ship(tmp_path, keys, receiver, sequence=1)

        final = worker.run_once()
        assert final.status_code is WorkerStatus.LIVE
        assert [e.status_code for e in worker.emissions] == [
            WorkerStatus.COMPOSED, WorkerStatus.GOLIVE_PASS, WorkerStatus.LIVE
        ]
        record = registry.get(bundle_id)
        assert record.status is SiteStatus.LIVE
        assert registry.live_for_hostname("site-000001.internal").bundle_id == bundle_id
        assert not (worker.inbox / bundle_id).exists(), "processed bundle left in the inbox"
        assert (worker.work_dir / bundle_id / "slots.json").is_file()
        assert registry.counters.as_metrics() == {"sites.live": 1, "sites.retired": 0}

    def test_emissions_carry_nothing_but_codes(self, tmp_path, keys, inside):
        receiver, worker, _ = inside
        ship(tmp_path, keys, receiver, sequence=1)
        worker.run_once()
        for emission in worker.emissions:
            assert set(vars(emission)) == {"bundle_id", "status_code", "subcode", "attempt"}
            assert isinstance(emission.subcode, int) and isinstance(emission.attempt, int)

    def test_superseding_revision_swaps_atomically(self, tmp_path, keys, inside):
        receiver, worker, registry = inside
        first = ship(tmp_path, keys, receiver, sequence=1)
        worker.run_once()
        second = ship(tmp_path, keys, receiver, sequence=2, revision=2, supersedes=first)
        worker.run_once()
        assert registry.get(first).status is SiteStatus.RETIRED
        assert registry.get(second).status is SiteStatus.LIVE
        assert registry.live_for_hostname("site-000001.internal").bundle_id == second
        assert registry.counters.as_metrics() == {"sites.live": 1, "sites.retired": 1}

    def test_inbox_is_consumed_in_sequence_order(self, tmp_path, keys, inside):
        receiver, worker, _ = inside
        # Received out of order is fine (gaps permitted); processed in order.
        later = ship(tmp_path, keys, receiver, sequence=5, revision=2)
        earlier = ship(tmp_path, keys, receiver, sequence=9, revision=3)
        assert worker.pending() == [later, earlier]
        assert worker.counters.inbox_depth == 2


class TestFailClosed:
    def test_failing_suite_does_not_go_live(self, tmp_path, keys, inside):
        receiver, worker, registry = inside

        def mutate(package: Path) -> None:
            suite = json.loads((package / "tests" / "suite.json").read_text())
            suite["tests"][0]["expect_status"] = 404
            (package / "tests" / "suite.json").write_text(json.dumps(suite))

        bundle_id = ship(tmp_path, keys, receiver, sequence=1, mutate=mutate)
        final = worker.run_once()
        assert final.status_code is WorkerStatus.GOLIVE_TEST_FAIL
        assert final.subcode == 0, "no retry rule applies; the worker must not improvise"
        assert final.attempt == 1
        assert registry.get(bundle_id) is None
        assert registry.live_for_hostname("site-000001.internal") is None

    def test_external_request_is_never_retried(self, tmp_path, keys, inside):
        receiver, worker, _ = inside

        def mutate(package: Path) -> None:
            home = package / "content" / "templates" / "home.html.j2"
            home.write_text(home.read_text().replace("</head>", '<script src="https://evil.example/x.js"></script></head>'))

        ship(tmp_path, keys, receiver, sequence=1, mutate=mutate)
        final = worker.run_once()
        assert (final.status_code, final.subcode) == (WorkerStatus.GOLIVE_TEST_FAIL, EXTERNAL_REQUEST)
        assert final.attempt == 1

    def test_supersedes_of_unknown_revision_is_refused_at_registration(self, tmp_path, keys, inside):
        receiver, worker, registry = inside
        ship(tmp_path, keys, receiver, sequence=1, revision=2, supersedes="site-000001-r1")
        with pytest.raises(AssertionError, match="not a registered revision"):
            worker.run_once()
        assert registry.live_sites() == []

    def test_worker_sees_only_ciphertext(self, tmp_path, keys, inside):
        """The deployment the worker assembles holds the blobs as they arrived."""
        receiver, worker, _ = inside
        bundle_id = ship(tmp_path, keys, receiver, sequence=1)
        worker.run_once()
        seed = json.loads((EXAMPLE / "spec" / "site.json").read_text())["db"]["seed_blob_ref"]
        plaintext = (EXAMPLE / seed).read_bytes()
        for blob in (worker.work_dir / bundle_id / "content").iterdir():
            assert blob.read_bytes() != plaintext
            assert b"SQLite format" not in blob.read_bytes()


class TestClassification:
    def test_example_spec_is_supported(self):
        spec = json.loads((EXAMPLE / "spec" / "site.json").read_text())
        assert classify_spec(spec).supported

    def test_unsupported_elements_are_named_by_id(self):
        spec = json.loads((EXAMPLE / "spec" / "site.json").read_text())
        spec["mutations"][0]["op"] = "upsert"
        spec["interactions"]["auth"] = "mock_session"
        spec["routes"].append({"id": "r_old", "path": "/old", "method": "GET", "redirect_to": "r_home"})
        assert classify_spec(spec).unsupported == ("r_old", "m_insert_reply", "interactions.auth")

    def test_unsupported_spec_is_code_21_subcode_1(self, tmp_path, keys, inside):
        receiver, worker, registry = inside

        def mutate(package: Path) -> None:
            spec = json.loads((package / "spec" / "site.json").read_text())
            spec["interactions"]["auth"] = "mock_session"
            (package / "spec" / "site.json").write_text(json.dumps(spec))

        ship(tmp_path, keys, receiver, sequence=1, mutate=mutate)
        final = worker.run_once()
        assert (final.status_code, final.subcode) == (WorkerStatus.COMPOSE_FAILED, UNSUPPORTED_ELEMENT)
        assert worker.counters.as_metrics()["worker.failed"] == 1
        assert registry.live_sites() == []

    def test_tier_framework_disagreement_is_a_mount_inconsistency(self, tmp_path, keys, inside):
        receiver, worker, _ = inside
        bundle_id = ship(tmp_path, keys, receiver, sequence=1)
        manifest_path = worker.inbox / bundle_id / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["tier"] = "B"  # the receiver already verified the signature; this is a bad mount
        manifest_path.write_text(json.dumps(manifest))
        final = worker.run_once()
        assert (final.status_code, final.subcode) == (WorkerStatus.COMPOSE_FAILED, MOUNT_INCONSISTENT)
