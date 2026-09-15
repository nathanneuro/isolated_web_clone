# `tests/suite.json`: test kinds

The authoritative schema is `schemas/suite.schema.json`. The suite is structure:
every value a test types or compares against is a fixture id resolved inside the
go-live sandbox from `content/fixtures.json`, which is encrypted like all content.

## Envelope

```json
{
  "suite_id": "<site_id>-r<revision>-tests",
  "runner": "playwright-v1",
  "fixture_blob_ref": "content/fixtures.json",
  "tests": [ ... ]
}
```

`suite_id` must match the manifest's site id and revision (`LINT-XREF-01`). Test
ids are `T` plus three or four digits and unique (`LINT-REF-04`). Fixture ids are
`fx_` plus up to 59 of `[a-z0-9_]`. Every `route`, `form`, `search`, and
`verify_query` a test names must exist in the spec (`LINT-XREF-02`).

## Kinds

| kind | required fields | optional | passes when |
|---|---|---|---|
| `route_ok` | `route`, `expect_status` (200, 302, 404) | `path_params_fixture` | GET returns exactly that status |
| `links_resolve` | `route`, `min_internal_links` (1–1000) | `path_params_fixture` | the page has at least that many internal links and each resolves with 200 |
| `search_returns` | `search`, `query_fixture`, `expect_min_results` (1–1000) | `expect_contains_fixture` | the search route returns 200 with at least that many `class="result-item"` and, if given, the expected document's text |
| `form_persists` | `form`, `route`, `input_fixture`, `verify_query`, `expect_row_count_delta` (−10..10) | `path_params_fixture` | POSTing the fixture returns 200, 302, or 303 and the verify query's row count changes by exactly the delta |
| `render_diff` | `route`, `reference_fixture`, `max_distance` (0–1] | `path_params_fixture` | *skipped inside* (needs a browser); scored by QA outside |
| `no_external_requests` | `route` | `path_params_fixture` | no `href`, `src`, or `action` on the rendered page points at `http://`, `https://`, or `//` |

`path_params_fixture` names a fixture whose value is an object of path param →
value, used to fill `{param}` segments in the route's path.

## What every site must carry

- `route_ok` for each route template, one representative instance
- `links_resolve` for the home route
- `search_returns` with a query fixture that is a substring of a real seed title,
  and `expect_contains_fixture` naming that title
- `no_external_requests` for every route
- `render_diff` for the home route, `max_distance` 0.35 unless you have a reason
- Tier A: `form_persists` for each mutation

QA checks this coverage (`QA-SUITE-01`). A suite that passes because it tests
nothing is the one failure inside can never detect.

## Result codes

The go-live service returns `{test_id, result_code}` pairs: 0 pass, 1 fail, 2
skipped, 3 runner error. You never see output inside; `recon-check` shows it to you
outside, which is why you run it first.
