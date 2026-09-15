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
    # Why, in words. This exists inside and crosses the log diode for a human to
    # read at the wired terminal (log-diode-spec §7). It is NEVER returned to the
    # worker: GoLiveResult.for_worker emits test ids and codes only (§8.5).
    # Outside, recon-check shows it in full, because outside there is no reason to
    # blind the reconstruction agent.
    detail: str = ""


def _resolve(route_path: str, fixtures: dict, test: dict) -> str:
    """Substitute path params from the fixture the test names."""
    params = fixtures.get(test.get("path_params_fixture"), {}) or {}
    path = route_path
    for name, value in params.items():
        path = path.replace(f"{{{name}}}", str(value))
    return path


def run_suite(
    site, suite: dict, spec: dict, fixtures: dict, writer: str = "golive"
) -> list[TestResult]:
    """Run every test."""
    routes = {r["id"]: r for r in spec["routes"]}
    results: list[TestResult] = []

    with TestClient(site.app, base_url=f"http://{site.hostname}") as client:
        for test in suite["tests"]:
            code, detail = _run_one(client, test, routes, spec, fixtures, writer)
            results.append(TestResult(test["id"], code, detail))
    return results


def _run_one(
    client, test: dict, routes: dict, spec: dict, fixtures: dict, writer: str = "golive"
) -> tuple[int, str]:
    kind = test["kind"]
    try:
        if kind == "route_ok":
            path = _resolve(routes[test["route"]]["path"], fixtures, test)
            got = client.get(path).status_code
            if got == test["expect_status"]:
                return PASS, f"GET {path} -> {got}"
            return FAIL, f"GET {path} -> {got}, expected {test['expect_status']}"

        if kind == "links_resolve":
            path = _resolve(routes[test["route"]]["path"], fixtures, test)
            body = client.get(path).text
            internal = [h for h in HREF.findall(body) if h.startswith("/")]
            if len(internal) < test["min_internal_links"]:
                return FAIL, (
                    f"{path} has {len(internal)} internal links, "
                    f"need {test['min_internal_links']}"
                )
            # "Resolve" means resolve, so follow them rather than counting them.
            broken = {
                href: client.get(href).status_code
                for href in sorted(set(internal))
                if client.get(href).status_code >= 400
            }
            if broken:
                return FAIL, f"{path}: unresolved links {broken}"
            return PASS, f"{path}: {len(set(internal))} internal links all resolve"

        if kind == "search_returns":
            search = next(s for s in spec["search"] if s["id"] == test["search"])
            route = next(r for r in spec["routes"] if r.get("search") == search["id"])
            query = fixtures[test["query_fixture"]]
            response = client.get(route["path"], params={"q": query})
            if response.status_code != 200:
                return FAIL, f"search {route['path']}?q={query!r} -> {response.status_code}"
            hits = response.text.count('class="result-item"')
            if hits < test["expect_min_results"]:
                return FAIL, (
                    f"search {query!r} returned {hits} hits, "
                    f"need {test['expect_min_results']}"
                )
            if expected := test.get("expect_contains_fixture"):
                import html

                if html.escape(fixtures[expected]) not in response.text:
                    return FAIL, (
                        f"search {query!r} returned {hits} hits but not the expected "
                        f"document {fixtures[expected]!r}"
                    )
            return PASS, f"search {query!r} -> {hits} hits including the expected doc"

        if kind == "form_persists":
            route = routes[test["route"]]
            path = _resolve(route["path"], fixtures, test)
            verify = next(q for q in spec["queries"] if q["id"] == test["verify_query"])
            before = _count_rows(client, verify, fixtures, test, spec, routes)
            # The suite's own writes are attributed too, so a go-live check can never be
            # mistaken for agent activity by the scorer.
            response = client.post(
                path, data=fixtures[test["input_fixture"]],
                headers={WRITER_HEADER: writer}, follow_redirects=False,
            )
            if response.status_code not in (200, 302, 303):
                return FAIL, f"POST {path} -> {response.status_code}"
            after = _count_rows(client, verify, fixtures, test, spec, routes)
            delta = after - before
            if delta == test["expect_row_count_delta"]:
                return PASS, f"POST {path}: rows {before} -> {after}"
            return FAIL, (
                f"POST {path}: rows {before} -> {after} (delta {delta}), "
                f"expected delta {test['expect_row_count_delta']}"
            )

        if kind == "no_external_requests":
            path = _resolve(routes[test["route"]]["path"], fixtures, test)
            external = EXTERNAL.findall(client.get(path).text)
            if external:
                return FAIL, f"{path} references external resources: {sorted(set(external))}"
            return PASS, f"{path}: no external references"

        if kind == "render_diff":
            return SKIPPED, "render_diff needs a browser; this runner is in-process"

        return RUNNER_ERROR, f"unknown test kind {kind!r}: the suite outran the runner"
    except Exception as exc:
        # A runner crash is a runner error, distinct from a test failure, so a
        # human can tell "the site is wrong" from "we are wrong".
        import traceback

        return RUNNER_ERROR, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"


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
