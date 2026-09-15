# The worker's interface

The worker is `tools.worker.Worker`. There is no separate CLI binary in this
repository; the commands the skill describes map onto its methods.

| skill command | method |
|---|---|
| `worker inbox next` | `pending()` → bundle ids, lowest sequence first |
| `worker load <bundle_id>` | done inside `process()`: reads `manifest.json`, `spec/site.json`, `tests/suite.json` |
| `compose-<framework> --dry-run` | `classify_spec(spec)` from `tools.compose_fastapi_sqlite_v1` |
| `compose-<framework> --out` | done inside `process()`: copies the verified bundle to `work/<bundle_id>/` and writes `slots.json` and `deploy.json` |
| `worker golive <bundle_id> <dir>` | `GoLiveService.go_live(deployment_dir)`, called by `process()` |
| `worker register ...` | `SiteRegistry.register(...)`, called by `process()` on `30` |
| `worker retire` | happens inside `register` when `supersedes` is set; or by dev command |

`run_once()` processes the next bundle and returns its final emission.
`emissions` holds every emission in order. `counters.as_metrics()` is the
`worker.*` block for the egress channel.

## Exit codes, as status codes

| code | name | subcodes |
|---|---|---|
| 20 | composed | |
| 21 | compose failed | 1 unsupported element, 2 slot map inconsistent, 3 excessive pattern flags, 4 mount inconsistency |
| 30 | go-live pass | |
| 31 | go-live test fail | 1 render_diff only, 2 external request, 3 retries exhausted, 4 runner error persisted |
| 32 | decrypt fail | |
| 33 | timeout | |
| 40 | live | |

## What the worker's filesystem view contains

`inbox/<bundle_id>/`: `manifest.json`, `manifest.sig`, `spec/`, `tests/`,
`content/*.blob`, `index/*.shard`. The blobs are ciphertext under a key the
worker does not hold. It copies them into the deployment as they are.

`work/<bundle_id>/`: the same, plus `slots.json` (blob handle → role and slot
pointer) and `deploy.json` (bundle id, site id, revision, framework, hostname,
supersedes).

The worker never opens the go-live sandbox, the registry file, or another
bundle's directory.
