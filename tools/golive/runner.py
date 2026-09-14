"""Run a test suite against a composed site, in-process.

The suite declares `runner: playwright-v1`. This is the in-process runner from
design-plan §3.2.6 (the AppWorld pattern), which covers every test kind that does
not need a real browser. `render_diff` needs one and is reported SKIPPED rather
than passed, because a runner that silently passes the tests it cannot perform is
exactly the "suite that passes because it tests nothing" that site-qa Q9 exists to
catch -- and inside the airgap nobody can read the logs to notice.

Results are `(test_id, result_code)` pairs. Never output, never diffs (§7).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from tools.compose_fastapi_sqlite_v1 import WRITER_HEADER

# schemas/status-codes.toml [test_result]
PASS, FAIL, SKIPPED, RUNNER_ERROR = 0, 1, 2, 3

HREF = re.compile(r'href="([^"]*)"')
EXTERNAL = re.compile(r'(?:href|src|action)="((?:https?:)?//[^"]*)"')


@dataclass(frozen=True)
class TestResult:
    test_id: str
    code: int


def _resolve(route_path: str, fixtures: dict, test: dict) -> str:
    """Substitute path params from the fixture the test names."""
    params = fixtures.get(test.get("path_params_fixture"), {}) or {}
    path = route_path
    for name, value in params.items():
        path = path.replace(f"{{{name}}}", str(value))
    return path


def run_suite(site, suite: dict, spec: dict, fixtures: dict) -> list[TestResult]:
    """Run every test. Returns codes only."""
    routes = {r["id"]: r for r in spec["routes"]}
    results: list[TestResult] = []

    with TestClient(site.app, base_url=f"http://{site.hostname}") as client:
        for test in suite["tests"]:
            results.append(TestResult(test["id"], _run_one(client, test, routes, spec, fixtures)))
    return results


def _run_one(client, test: dict, routes: dict, spec: dict, fixtures: dict) -> int:
    kind = test["kind"]
    try:
        if kind == "route_ok":
            path = _resolve(routes[test["route"]]["path"], fixtures, test)
            return PASS if client.get(path).status_code == test["expect_status"] else FAIL

        if kind == "links_resolve":
            path = _resolve(routes[test["route"]]["path"], fixtures, test)
            body = client.get(path).text
            internal = [h for h in HREF.findall(body) if h.startswith("/")]
            if len(internal) < test["min_internal_links"]:
                return FAIL
            # "Resolve" means resolve, so follow them rather than counting them.
            return PASS if all(
                client.get(href).status_code < 400 for href in set(internal)
            ) else FAIL

        if kind == "search_returns":
            search = next(s for s in spec["search"] if s["id"] == test["search"])
            route = next(r for r in spec["routes"] if r.get("search") == search["id"])
            response = client.get(route["path"], params={"q": fixtures[test["query_fixture"]]})
            if response.status_code != 200:
                return FAIL
            hits = response.text.count('class="result-item"')
            if hits < test["expect_min_results"]:
                return FAIL
            if expected := test.get("expect_contains_fixture"):
                import html

                if html.escape(fixtures[expected]) not in response.text:
                    return FAIL
            return PASS

        if kind == "form_persists":
            route = routes[test["route"]]
            path = _resolve(route["path"], fixtures, test)
            verify = next(q for q in spec["queries"] if q["id"] == test["verify_query"])
            before = _count_rows(client, verify, fixtures, test, spec, routes)
            # The suite's own writes are attributed too, so a go-live check can never be
            # mistaken for agent activity by the scorer.
            response = client.post(
                path, data=fixtures[test["input_fixture"]],
                headers={WRITER_HEADER: "golive"}, follow_redirects=False,
            )
            if response.status_code not in (200, 302, 303):
                return FAIL
            after = _count_rows(client, verify, fixtures, test, spec, routes)
            return PASS if after - before == test["expect_row_count_delta"] else FAIL

        if kind == "no_external_requests":
            path = _resolve(routes[test["route"]]["path"], fixtures, test)
            return PASS if not EXTERNAL.findall(client.get(path).text) else FAIL

        if kind == "render_diff":
            return SKIPPED  # needs a browser; see the module docstring

        return RUNNER_ERROR  # unknown kind: the suite outran the runner
    except Exception:
        # A runner crash is a runner error, distinct from a test failure, so a
        # human at the terminal can tell "the site is wrong" from "we are wrong".
        return RUNNER_ERROR


def _count_rows(client, verify_query, fixtures, test, spec, routes) -> int:
    """Count rows the verify_query would return, via the served page.

    Going through HTTP rather than straight to SQLite is deliberate: it verifies
    what the agent would actually observe.
    """
    route = next(
        r for r in spec["routes"]
        if verify_query["id"] in r.get("queries", []) and r["method"] == "GET"
    )
    path = _resolve(route["path"], fixtures, test)
    return client.get(path).text.count('class="reply-item"')
