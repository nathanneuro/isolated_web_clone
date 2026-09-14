"""Compose + go-live: does the content stay where it belongs, and do the codes mean
what they say?

The go-live service is the only component inside that can read site content, so the
tests that matter are the ones asserting what it does NOT hand back.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from nacl.public import PrivateKey

from tools.bundle_build import build_bundle
from tools.bundle_build.keys import KeyRole, generate_demo_keyset, load_signing_identity
from tools.compose_fastapi_sqlite_v1 import ComposeError, compose_app
from tools.compose_fastapi_sqlite_v1.compose import Bm25Shard, _build_query
from tools.golive import GoLiveService
from tools.golive.runner import PASS, SKIPPED
from tools.golive.service import GoLiveStatus

EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"


@pytest.fixture(scope="module")
def deployed(tmp_path_factory):
    """A bundle built and unpacked as the receiver would leave it."""
    root = tmp_path_factory.mktemp("golive")
    keys = root / "keys"
    generate_demo_keyset(keys)
    identity = load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)
    archive = build_bundle(
        EXAMPLE, root / "out", identity=identity,
        golive_public_key_path=keys / "golive-wrapping.pub", sequence=1,
    )
    return keys, archive.parent / archive.name.removesuffix(".tar")


class TestGoLive:
    def test_example_site_passes(self, deployed, tmp_path):
        keys, deployment = deployed
        result = GoLiveService(keys / "golive-wrapping.key", tmp_path / "sb").go_live(deployment)
        assert result.status is GoLiveStatus.PASS
        assert {t.code for t in result.tests} <= {PASS, SKIPPED}

    def test_render_diff_is_skipped_not_passed(self, deployed, tmp_path):
        """A runner that passes tests it cannot perform is worse than one that
        admits it. Inside, nobody can read a log to notice."""
        keys, deployment = deployed
        result = GoLiveService(keys / "golive-wrapping.key", tmp_path / "sb").go_live(deployment)
        render = next(t for t in result.tests if t.test_id == "T007")
        assert render.code == SKIPPED

    def test_wrong_key_is_decrypt_fail_not_a_crash(self, deployed, tmp_path):
        keys, deployment = deployed
        wrong = tmp_path / "wrong.key"
        wrong.write_bytes(bytes(PrivateKey.generate()))
        result = GoLiveService(wrong, tmp_path / "sb2").go_live(deployment)
        assert result.status is GoLiveStatus.DECRYPT_FAIL

    def test_worker_payload_carries_no_detail(self, deployed, tmp_path):
        """§8.5: codes back to the worker, never output, never decrypted anything."""
        keys, deployment = deployed
        result = GoLiveService(keys / "golive-wrapping.key", tmp_path / "sb3").go_live(deployment)
        payload = result.for_worker()
        assert set(payload) == {"status", "tests"}
        blob = json.dumps(payload)
        assert "Longhouse" not in blob
        for entry in payload["tests"]:
            assert isinstance(entry[1], int)

    def test_failing_suite_destroys_the_sandbox(self, deployed, tmp_path):
        """§8.5.7: on fail, destroy it. A half-live site is nobody's friend."""
        keys, deployment = deployed
        broken = tmp_path / "broken"
        shutil.copytree(deployment, broken)
        suite = json.loads((broken / "tests" / "suite.json").read_text())
        suite["tests"][0]["expect_status"] = 404  # home page is 200; force a failure
        (broken / "tests" / "suite.json").write_text(json.dumps(suite))
        sandboxes = tmp_path / "sb4"
        result = GoLiveService(keys / "golive-wrapping.key", sandboxes).go_live(broken)
        assert result.status is GoLiveStatus.TEST_FAIL
        assert not (sandboxes / "site-000001-r1").exists()


class TestCompose:
    @pytest.fixture
    def site(self, tmp_path):
        work = tmp_path / "content"
        shutil.copytree(EXAMPLE / "content", work / "content")
        shutil.copytree(EXAMPLE / "index", work / "index")
        spec = json.loads((EXAMPLE / "spec" / "site.json").read_text())
        return compose_app(spec, work, work / "content" / "seed.sqlite")

    def test_rejects_a_framework_it_does_not_build(self, tmp_path):
        with pytest.raises(ComposeError, match="fastapi-sqlite-v1"):
            compose_app({"framework": "django-postgres-v1"}, tmp_path, tmp_path / "db")

    def test_serves_pages_and_search(self, site):
        from fastapi.testclient import TestClient

        with TestClient(site.app, base_url=f"http://{site.hostname}") as client:
            assert client.get("/").status_code == 200
            assert client.get("/thread/1").status_code == 200
            assert client.get("/search", params={"q": "the"}).status_code == 200
            assert client.get("/static/style.css").status_code == 200
            assert client.get("/static/../../etc/passwd").status_code == 404

    def test_unknown_thread_is_404_not_a_crash(self, site):
        from fastapi.testclient import TestClient

        with TestClient(site.app, base_url=f"http://{site.hostname}") as client:
            assert client.get("/thread/999999").status_code == 404

    def test_page_text_is_escaped(self, site):
        """Autoescape on. Seed content is untrusted text, not markup."""
        assert site.app is not None
        from fastapi.testclient import TestClient

        with TestClient(site.app, base_url=f"http://{site.hostname}") as client:
            body = client.get("/").text
            assert "<script>" not in body


class TestQueryCompilation:
    """Values bind; identifiers interpolate. Getting that backwards is the bug."""

    def test_where_values_become_bound_parameters(self):
        sql, params = _build_query({"id": "q", "table": "posts", "where": {"id": "{post_id}"}})
        assert sql == "SELECT * FROM posts WHERE id = ?"
        assert params == ["post_id"]

    def test_literal_where_value_is_refused(self):
        """The schema forbids it; if one arrives anyway, do not embed it."""
        with pytest.raises(ComposeError, match="param reference"):
            _build_query({"id": "q", "table": "posts", "where": {"id": "5 OR 1=1"}})

    def test_hostile_table_name_is_refused(self):
        with pytest.raises(ComposeError, match="unsafe table"):
            _build_query({"id": "q", "table": "posts; DROP TABLE posts"})

    def test_order_and_limit_compile(self):
        sql, _ = _build_query(
            {"id": "q", "table": "posts", "order_by": "created_at desc", "limit": 20}
        )
        assert sql.endswith("ORDER BY created_at DESC LIMIT 20")


class TestSearchShard:
    def test_shard_ranks_by_score(self):
        """Query a term drawn from the index itself. The corpus is Faker's word
        list, which has no articles, so 'the' would find nothing and prove nothing."""
        shard = Bm25Shard(EXAMPLE / "index" / "main.shard")
        term = max(shard.postings, key=lambda t: len(shard.postings[t]))
        hits = shard.search(term)
        assert hits, f"no hits for {term!r}, the most common term in the index"
        assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)

    def test_unknown_term_returns_nothing_rather_than_everything(self):
        shard = Bm25Shard(EXAMPLE / "index" / "main.shard")
        assert shard.search("zzzznotaword") == []

    def test_title_query_finds_its_own_document(self):
        """What the search_returns test kind relies on."""
        shard = Bm25Shard(EXAMPLE / "index" / "main.shard")
        doc_id, title = next(iter(shard.titles.items()))
        assert int(doc_id) in {h["id"] for h in shard.search(title)}

    def test_unknown_shard_format_is_refused(self, tmp_path):
        bad = tmp_path / "bad.shard"
        bad.write_text(json.dumps({"format": "lucene-v9"}))
        with pytest.raises(ComposeError, match="unsupported shard format"):
            Bm25Shard(bad)
