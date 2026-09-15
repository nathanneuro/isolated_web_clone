# Retry rules: failing test kind → permitted change

You get at most 3 attempts. Your signal is the list of `{test_id, result_code}`
pairs, and `kind` for each test id from `tests/suite.json`. Nothing else.

| what failed | rule | status if it stands |
|---|---|---|
| any `no_external_requests` | never retry; a template reaches out and the site must not go live | `31` subcode `2` |
| only `render_diff` | never retry; soft signal, a human decides on the threshold | `31` subcode `1` |
| any test with result `3` (runner error) | retry once, unchanged; if it recurs | `31` subcode `4` |
| `route_ok`, `links_resolve`, `search_returns`, `form_persists` | would map to an alternate pattern choice, and the generator has no alternates; do not improvise | `31` subcode `0` |
| attempts exhausted | | `31` subcode `3` |

Go-live statuses that are not test failures:

| status | rule |
|---|---|
| `30` pass | register (`40`) |
| `32` decrypt fail | not yours; mark `32` |
| `33` timeout | retry once, unchanged; if it recurs mark `33` |
| `21` from go-live | the generator refused something classification passed; mark `21` subcode `1` |

Every emission is `{bundle_id, status_code, subcode, attempt}`.
