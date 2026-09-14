"""Golden fixtures for the lint tests.

The spec below is the shape bundle-format-spec §6 documents, with invented
identifiers. It contains no scraped content, and by design it contains no content at
all: every string here is an identifier, an enum, a path, or a blob handle.
"""

from __future__ import annotations

import copy

import pytest

H = {
    "template": "a" * 64,
    "asset": "b" * 64,
    "seed": "c" * 64,
    "shard": "d" * 64,
    "fixtures": "e" * 64,
    "template_post": "f" * 64,
    "template_results": "0" * 64,
}

MANIFEST_FILES = {
    "spec/site.json": "spec",
    "tests/suite.json": "suite",
    f"content/{H['template']}.blob": "template",
    f"content/{H['template_post']}.blob": "template",
    f"content/{H['template_results']}.blob": "template",
    f"content/{H['asset']}.blob": "asset",
    f"content/{H['seed']}.blob": "db_seed",
    f"content/{H['fixtures']}.blob": "fixtures",
    f"index/{H['shard']}.shard": "bm25_shard",
}

_SPEC = {
    "site_id": "site-000417",
    "tier": "A",
    "framework": "fastapi-sqlite-v1",
    "hostname": "site-000417.internal",
    "routes": [
        {"id": "r_home", "path": "/", "method": "GET", "template": "t_home",
         "queries": ["q_recent_posts"]},
        {"id": "r_search", "path": "/search", "method": "GET",
         "template": "t_results", "search": "s_main"},
        {"id": "r_post", "path": "/post/{post_id}", "method": "GET",
         "template": "t_post", "queries": ["q_post_by_id", "q_comments_for_post"]},
        {"id": "r_comment", "path": "/post/{post_id}/comment", "method": "POST",
         "form": "f_comment", "mutation": "m_insert_comment"},
    ],
    "templates": [
        {"id": "t_home", "blob_ref": f"content/{H['template']}.blob", "engine": "jinja2"},
        {"id": "t_post", "blob_ref": f"content/{H['template_post']}.blob", "engine": "jinja2"},
        {"id": "t_results", "blob_ref": f"content/{H['template_results']}.blob", "engine": "jinja2"},
    ],
    "db": {
        "tables": [
            {"name": "posts", "columns": [
                {"name": "id", "type": "integer", "pk": True},
                {"name": "title", "type": "text"},
                {"name": "body", "type": "text"},
                {"name": "created_at", "type": "timestamp", "indexed": True},
            ]},
            {"name": "comments", "columns": [
                {"name": "id", "type": "integer", "pk": True},
                {"name": "post_id", "type": "integer", "fk": "posts.id"},
                {"name": "body", "type": "text"},
                {"name": "author", "type": "text", "nullable": True},
            ]},
        ],
        "seed_blob_ref": f"content/{H['seed']}.blob",
        "seed_format": "sqlite-dump-v1",
    },
    "queries": [
        {"id": "q_recent_posts", "table": "posts", "order_by": "created_at desc", "limit": 20},
        {"id": "q_post_by_id", "table": "posts", "where": {"id": "{post_id}"}},
        {"id": "q_comments_for_post", "table": "comments", "where": {"post_id": "{post_id}"}},
    ],
    "forms": [
        {"id": "f_comment", "fields": [
            {"name": "body", "type": "textarea", "required": True, "max_length": 4096},
            {"name": "author", "type": "text", "required": False},
        ]},
    ],
    "mutations": [
        {"id": "m_insert_comment", "table": "comments", "op": "insert",
         "from_form": "f_comment", "bind": {"post_id": "{post_id}"}},
    ],
    "search": [
        {"id": "s_main", "kind": "bm25", "tables": ["posts"],
         "fields": ["title", "body"], "shard_refs": [f"index/{H['shard']}.shard"]},
    ],
    "interactions": {"auth": "none", "payments": "dead_end", "realtime": "none"},
    "assets": [{"path": "/static/style.css", "blob_ref": f"content/{H['asset']}.blob"}],
}

_SUITE = {
    "suite_id": "site-000417-r3-tests",
    "runner": "playwright-v1",
    "fixture_blob_ref": f"content/{H['fixtures']}.blob",
    "tests": [
        {"id": "T001", "kind": "route_ok", "route": "r_home", "expect_status": 200},
        {"id": "T002", "kind": "links_resolve", "route": "r_home", "min_internal_links": 5},
        {"id": "T003", "kind": "search_returns", "search": "s_main",
         "query_fixture": "fx_q1", "expect_min_results": 3,
         "expect_contains_fixture": "fx_q1_doc"},
        {"id": "T004", "kind": "form_persists", "form": "f_comment", "route": "r_comment",
         "path_params_fixture": "fx_post_1", "input_fixture": "fx_comment_1",
         "verify_query": "q_comments_for_post", "expect_row_count_delta": 1},
        {"id": "T005", "kind": "render_diff", "route": "r_home",
         "reference_fixture": "fx_home_png", "max_distance": 0.35},
        {"id": "T006", "kind": "no_external_requests", "route": "r_home"},
    ],
}


@pytest.fixture
def spec() -> dict:
    return copy.deepcopy(_SPEC)


@pytest.fixture
def suite() -> dict:
    return copy.deepcopy(_SUITE)


@pytest.fixture
def manifest_files() -> dict[str, str]:
    return dict(MANIFEST_FILES)
