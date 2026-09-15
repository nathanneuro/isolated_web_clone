# `spec/site.json` for QA

The full reference is `../../site-reconstruct/references/spec-schema.md`; read
that. This page is what Q2 and Q4 need from it.

## Q2: which strings are vocabulary

A string in the spec is legitimate if it is one of:

- an id matching `^[a-z][a-z0-9_]{0,62}$` that names a route, template, query,
  form, mutation, or search, by a generic kind-prefixed name (`r_`, `t_`, `q_`,
  `f_`, `m_`, `s_`)
- a table or column name that says what the data is (`posts`, `title`, `author`,
  `created_at`), not what the original site called it
- a route path built from literal segments and `{param}` placeholders; a literal
  segment that is a real slug from the site is leakage
- an enum value from the schema (`GET`, `jinja2`, `bm25`, `dead_end`, ...)
- a blob handle (after build) or a logical path under `content/` or `index/`
  (before build)
- a `{param}` reference in a `where` or `bind`

Anything else is either lint's job (`LINT-TEXT-*`) or yours (`QA-LEAK-01`,
`QA-LEAK-02`). Lint catches strings over 64 characters or 4 words. You catch the
short ones.

## Q4: what the vocabulary can express

A `dead_end` declaration is legitimate only for interactions the schema cannot
express. It can express:

- any navigation between pages (routes with templates and queries)
- listing and detail views over seeded tables, ordered and limited
- a site search box (`search`, one BM25 shard)
- form submission that inserts a row (`insert`), edits a row the requester wrote
  (`update`), or deletes one (`delete`), bound to the route's path params
- select fields whose options come from a query

It cannot express: login and sessions (`auth` other than `none` or `dead_end`),
payments, live updates, joins across tables, redirects, filtering by a literal
value, and anything that needs code. An explorer-exercised interaction in the
first list declared `dead_end` is `QA-AFF-03`; in the second list it is correct.
