# The local browser harness

What the repository ships for Q4 through Q6 is `recon-check`, which deploys a
package with the same generator the inside worker uses and runs the suite
unencrypted:

```
uv run python -m tools.recon_check /qa/<site_id>/out
```

It reports each test's id, kind, and result, plus lint findings by code and
location, and unsupported elements by id. It is the same runner go-live uses
inside; the only difference is that you get names.

## Serving for a walk-through

`recon-check` composes the site in-process. To browse it, compose it yourself and
serve it locally:

```python
from pathlib import Path
import json, shutil, uvicorn
from tools.compose_fastapi_sqlite_v1 import compose_app

pkg = Path("/qa/site-000417/out")
work = Path("/tmp/qa-site"); shutil.copytree(pkg, work, dirs_exist_ok=True)
spec = json.loads((work / "spec/site.json").read_text())
site = compose_app(spec, work, work / spec["db"]["seed_blob_ref"])
uvicorn.run(site.app, host="127.0.0.1", port=8080)
```

Every POST must carry an `x-writer` header (any identifier; use `qa`), or the
site returns 400. That is the write-attribution rule and it applies outside too.

## Capturing outbound requests (Q6)

The composed app makes no outbound requests itself; what you are looking for is
what a *browser* would fetch. Two layers:

1. Static: grep templates and assets for the constructs listed in
   `../../site-reconstruct/references/template-subset.md`. Every hit is
   `QA-ISO-02`.
2. Dynamic: point a real browser at the served site with a proxy or the browser's
   devtools network log, visit every route, and record any request whose host is
   not `127.0.0.1:8080` or whose path is not under `/static/`. Any is `QA-ISO-01`.
   The repository does not ship a Playwright driver; the `no_external_requests`
   test inside is the static layer only, which is why the dynamic layer is your
   job here.

## Screenshots for Q7 and `render_diff`

`render_diff` is skipped inside (no browser). Reference screenshots come from
`queue/scrape/pages/`, never regenerated. Compare the served page to the reference
with whatever perceptual metric your harness uses, against the test's
`max_distance`. A regenerated reference is `QA-FIX-01`.
