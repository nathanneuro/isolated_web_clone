# Generator patterns

Dry-run classification is `tools.compose_fastapi_sqlite_v1.classify_spec`. It
returns element ids the generator cannot build (UNSUPPORTED) and pattern ids
that need a choice (FLAGGED).

## There are no FLAGGED patterns

Today every element is either supported by the one implementation of it or
unsupported. When the generator grows a second way to build something, the
choice appears here as a pattern id with options and a decision rule, and the
worker's chooser hook is where it gets made. Until then, a FLAGGED result is a
generator bug; the worker marks the bundle `21` with subcode `3`.

## What classifies UNSUPPORTED

| element | condition |
|---|---|
| `framework` | anything but `fastapi-sqlite-v1` (Tier B has no generator yet) |
| a route | has `redirect_to`; or is `GET` without `template`; or `POST` without `mutation`; or a method outside `GET`, `POST` |
| a template | `engine` other than `jinja2` |
| a query | has `join` |
| a mutation | `op` outside `insert`, `update`, `delete`; or `update`/`delete` without `bind` |
| a search | `kind` other than `bm25`, or not exactly one `shard_refs` entry |
| `interactions.auth` | `mock_session` |
| `interactions.realtime` | `poll_stub` |

An UNSUPPORTED result is `21` subcode `1`. Do not work around it.

## What the one implementation does

- `GET` route: runs its queries with the path params, runs its search with `q`,
  renders the template with autoescape, 404 if a single-row query finds nothing.
- `POST` route: requires an `x-writer` header; `insert` writes the form's fields
  plus the bound path params plus `created_at`, redirects to the parent path;
  `update` and `delete` act on the row the bind names *and the writer owns*, 404
  otherwise.
- Every table a mutation names gets a `writer` column added at compose time.
- Assets are served only at declared `/static/` paths.
