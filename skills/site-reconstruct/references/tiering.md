# Tiering: how a site is assigned A or B, and what each may contain

`tier.txt` in your work directory is decided upstream, before you see the site.
You do not change it. It determines the framework, and lint checks that the two
agree (`LINT-SCHEMA-03`).

| | Tier A | Tier B |
|---|---|---|
| framework | `fastapi-sqlite-v1` | `static-v1` |
| what it is | a functional rebuild: routes, queries, forms, mutations, search | static-served captures with a working search box |
| interactions | read, search, write | read, search |
| forms and mutations | yes | none; every write interaction is `dead_end` |
| tests | route_ok, links_resolve, search_returns, form_persists, render_diff, no_external_requests | the same minus form_persists |
| generator status | implemented (`tools/compose_fastapi_sqlite_v1`) | not implemented; a Tier B bundle classifies UNSUPPORTED inside today |

## How upstream decides

The rule of thumb upstream applies, so you can predict it:

- Top-N sites by expected task volume, and any site whose explorer session found
  state-changing interactions that matter for tasks (posting, commenting, carts,
  settings), are Tier A.
- Long-tail sites, and sites where the explorer found only navigation and search,
  are Tier B.

## What Tier B changes for you

- No `forms`, `mutations`, or `POST` routes in the spec. The write interactions
  the explorer saw are declared `dead_end`; the route exists and renders the
  dead-end template.
- `db` still exists: Tier B pages are rendered from a seed too, so the search box
  has something to index and detail pages have content. The difference is that
  nothing writes to it.
- The `search` entry is still mandatory. Both tiers get one; the fake-web engine
  relies on per-site search being present.

Until `compose-static-v1` exists, a Tier B package can be linted and QA'd
outside but cannot go live inside. Build it correctly anyway; it will be picked
up when the generator lands.
