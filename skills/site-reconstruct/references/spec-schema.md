# `spec/site.json`: allowed keys and enums

The authoritative schema is `schemas/site.schema.json`; `bundle-lint` validates
against it. This page is the same information arranged for writing a spec. Every
string is an identifier, an enum, a path, or a blob handle. `additionalProperties`
is false everywhere: an unknown key is `LINT-SCHEMA-02`.

## Identifier shapes

| shape | pattern | used for |
|---|---|---|
| id | `^[a-z][a-z0-9_]{0,62}$` | route, template, query, form, mutation, search ids |
| site_id | `^site-[0-9]{6}$` | |
| hostname | `^[a-z0-9][a-z0-9.-]{0,61}\.internal$` | |
| route_path | `^/[A-Za-z0-9_\-./{}]{0,127}$` | `{param}` segments are path params |
| asset_path | `^/static/[A-Za-z0-9_\-./]{1,127}$` | |
| table_name, column_name | `^[a-z][a-z0-9_]{0,62}$` | |
| param_ref | `^\{[a-z][a-z0-9_]{0,62}\}$` | the only thing a `where` or `bind` value may be |
| order_by | `^<column> (asc|desc)$` | |
| blob_ref | `^(content|index)/[0-9a-f]{64}\.(blob|shard)$` | after `bundle-build`; you write logical paths, it rewrites them |

Convention, enforced by QA (`QA-LEAK-01`): ids are generic (`r_post`, `t_home`,
`q_recent_posts`), prefixed by kind (`r_`, `t_`, `q_`, `f_`, `m_`, `s_`), and never
derived from site text.

## Top level

| key | required | value |
|---|---|---|
| `site_id` | yes | site_id |
| `tier` | yes | `"A"` or `"B"` (see `tiering.md`) |
| `framework` | yes | `"fastapi-sqlite-v1"` (Tier A) or `"static-v1"` (Tier B); must match the tier or `LINT-SCHEMA-03` |
| `hostname` | yes | hostname |
| `routes` | yes, ≥1 | see below |
| `templates` | | |
| `db` | | |
| `queries` | | |
| `forms` | | |
| `mutations` | | |
| `search` | | |
| `interactions` | yes | |
| `assets` | | |

## `routes[]`

`{id, path, method, template?, queries?, search?, form?, mutation?, redirect_to?}`

- `method` is `GET` or `POST`.
- A `GET` route needs a `template`. It may list `queries` (each run and passed to the
  template under its query id) and one `search` (its results passed under the search
  id, for the `q` query parameter).
- A `POST` route needs a `mutation`; `form` names the form it accepts.
- `redirect_to` is in the schema but no generator supports it yet: it classifies as
  UNSUPPORTED (see `../../inside-worker/references/patterns.md`).

A query whose `where` binds `id` to a path param renders as one object; any other
query renders as a list. The template knows which.

## `templates[]`

`{id, blob_ref, engine}` with `engine` in `jinja2`, `html`. Only `jinja2` is
supported by the Tier A generator. Templates are content: write the file under
`content/templates/` and put its logical path in `blob_ref`. See
`template-subset.md`.

## `db`

`{tables: [...], seed_blob_ref, seed_format: "sqlite-dump-v1"}`

Each table: `{name, columns: [{name, type, pk?, fk?, nullable?, indexed?}]}` with
`type` in `integer`, `text`, `real`, `blob`, `timestamp`, `boolean` and `fk` as
`table.column`.

Do not declare a `writer` column. The composer adds one to every table a mutation
names, at compose time, for write attribution. Declaring it yourself is not an
error but is redundant.

## `queries[]`

`{id, table, order_by?, limit?, where?, join?}`

- `where` maps column → `{param}`. A literal value here is content and is refused
  by the schema. There is no way to write `WHERE status = 'published'`; if the
  original site filtered that way, seed only the published rows.
- `limit` is 1 to 1000.
- `join` is in the schema but unsupported by the generator.

## `forms[]`

`{id, fields: [{name, type, required?, options_query?, max_length?}]}` with at
least one field. `type` in `text`, `textarea`, `number`, `checkbox`, `select`,
`hidden`. A `select` takes its options from `options_query`, never inline.

## `mutations[]`

`{id, table, op, from_form, bind?}`

- `op` in `insert`, `update`, `delete`. All three are supported.
- `bind` maps column → `{param}` from the route's path. `update` and `delete`
  require a bind, because the path is what names the row.
- `update` and `delete` additionally require that the row's `writer` equals the
  requester's attribution. A user can only change what they wrote. A row that is
  missing and a row somebody else wrote both return 404.
- Every `POST` must carry an `x-writer` header naming its writer. Inside, the env
  broker, the population driver, and go-live set this. Your `recon-check` runner
  sets it too. A request without one is a 400.

## `search[]`

`{id, kind: "bm25", tables: [table...], fields: [column...], shard_refs: [path]}`

Exactly one shard per search is supported. `bundle-build` produces the shard from
the seed; you declare the tables and fields it indexes.

## `interactions`

`{auth, payments, realtime}`, all required.

| key | values | supported |
|---|---|---|
| `auth` | `none`, `mock_session`, `dead_end` | `none`, `dead_end` |
| `payments` | `none`, `dead_end` | both |
| `realtime` | `none`, `poll_stub` | `none` |

Anything unsupported classifies the whole spec UNSUPPORTED and the worker marks
the bundle `21`. Declare `dead_end` for what the schema cannot express.

## `assets[]`

`{path, blob_ref}` with `path` under `/static/`. Only assets you declare are
served; a template that references an undeclared one gets a 404 for it, and a
template that references anything outside `/static/` fails `no_external_requests`.
