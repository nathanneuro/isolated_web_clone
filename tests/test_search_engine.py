"""The fake-web search engine: across sites, from the registry, read-only."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from nacl.signing import VerifyKey

from tools.bundle_build import build_bundle
from tools.bundle_build.keys import KeyRole, generate_demo_keyset, load_signing_identity
from tools.egress.registry import load_registry
from tools.golive import GoLiveService
from tools.receiver import Receiver
from tools.registry import SiteRegistry
from tools.search_engine import FakeWebSearch
from tools.worker import Worker

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
    registry = SiteRegistry(tmp_path / "registry.json")
    worker = Worker(tmp_path / "inbox", tmp_path / "work", GoLiveService(keys / "golive-wrapping.key", tmp_path / "sandboxes"), registry)
    return receiver, worker, registry


def go_live(tmp_path, keys, receiver, worker, site_number: int, sequence: int) -> str:
    """Ship the example site under another site id, so two sites can be live."""
    site_id = f"site-{site_number:06d}"
    package = tmp_path / f"pkg-{site_id}"
    shutil.copytree(EXAMPLE, package)
    meta = json.loads((package / "build.json").read_text())
    meta["site_id"] = site_id
    (package / "build.json").write_text(json.dumps(meta))
    spec = json.loads((package / "spec" / "site.json").read_text())
    spec["site_id"], spec["hostname"] = site_id, f"{site_id}.internal"
    (package / "spec" / "site.json").write_text(json.dumps(spec))
    population = json.loads((package / "spec" / "population.json").read_text())
    population["site_id"], population["population_id"] = site_id, f"{site_id}-pop-r1"
    (package / "spec" / "population.json").write_text(json.dumps(population))
    suite = json.loads((package / "tests" / "suite.json").read_text())
    suite["suite_id"] = f"{site_id}-r1-tests"
    (package / "tests" / "suite.json").write_text(json.dumps(suite))
    identity = load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)
    archive = build_bundle(package, tmp_path / f"out-{site_id}", identity=identity,
                           golive_public_key_path=keys / "golive-wrapping.pub", sequence=sequence)
    assert receiver.receive(archive).accepted
    assert worker.run_once().status_code == 40
    return site_id


def a_title(tmp_path, site_id) -> str:
    import sqlite3

    db = tmp_path / f"pkg-{site_id}" / "content" / "seed.sqlite"
    return sqlite3.connect(db).execute("SELECT title FROM threads WHERE id = 3").fetchone()[0]


class TestAcrossSites:
    def test_mounts_live_sites_and_finds_documents_on_each(self, tmp_path, keys, inside):
        receiver, worker, registry = inside
        first = go_live(tmp_path, keys, receiver, worker, 1, 1)
        second = go_live(tmp_path, keys, receiver, worker, 2, 2)
        engine = FakeWebSearch(registry, clock=lambda: 100.0)
        assert engine.refresh() == 2
        assert engine.sites == [first, second]

        hits = engine.search(a_title(tmp_path, first), limit=5)
        assert hits, "a seed title must find its own document"
        assert {h.site_id for h in hits} == {first, second}, "the same content is live on both sites"
        top = hits[0]
        assert top.doc_id == 3 and top.path == "/thread/3" and top.hostname.endswith(".internal")
        assert hits == sorted(hits, key=lambda h: (-h.score, h.site_id, h.doc_id))

    def test_retired_sites_leave_the_index_on_refresh(self, tmp_path, keys, inside):
        receiver, worker, registry = inside
        first = go_live(tmp_path, keys, receiver, worker, 1, 1)
        engine = FakeWebSearch(registry)
        engine.refresh()
        registry.retire(f"{first}-r1")
        assert engine.refresh() == 0
        assert engine.search("anything") == []

    def test_metrics_are_registered_integers(self, tmp_path, keys, inside):
        receiver, worker, registry = inside
        go_live(tmp_path, keys, receiver, worker, 1, 1)
        engine = FakeWebSearch(registry, clock=lambda: 100.0)
        engine.refresh()
        engine.search("x")
        metrics = engine.counters.as_metrics()
        for name, value in metrics.items():
            load_registry().by_name(name)
            assert isinstance(value, int)
        assert metrics["search.indexed_sites"] == 1

    def test_engine_reads_the_sandbox_not_the_bundle(self, tmp_path, keys, inside):
        receiver, worker, registry = inside
        go_live(tmp_path, keys, receiver, worker, 1, 1)
        shutil.rmtree(tmp_path / "work")  # the worker's deployment copies (ciphertext) are gone
        engine = FakeWebSearch(registry)
        assert engine.refresh() == 1
