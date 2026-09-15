# Templates: the permitted Jinja2 subset and asset rules

Templates are content. They are encrypted with everything else under `content/`,
rendered inside a serving sandbox by the Tier A generator, and never read by any
LLM inside. That is why they may contain page chrome text; it is also why they
must not contain anything that reaches out.

## Engine

`jinja2`, with autoescape on for every template. `{{ value }}` is HTML-escaped.
There is no way to turn autoescape off from inside a template that the generator
honours, so do not try; text that looks like markup in the seed renders as text.

## Permitted

- Variable output: `{{ q_post_by_id.title }}`, `{{ site_name }}`, `{{ query }}`
- Loops: `{% for row in q_recent_posts %} ... {% endfor %}`
- Conditionals: `{% if ... %} ... {% elif ... %} ... {% else %} ... {% endif %}`
- Filters that ship with Jinja2 and take no external input: `length`, `upper`,
  `lower`, `truncate`, `default`, `join`, `first`, `last`

## Not permitted

- `{% include %}`, `{% import %}`, `{% extends %}`, `{% macro %}` referencing
  anything but the template ids in the spec. The generator loads templates from
  an in-memory dictionary keyed by template id; there is no filesystem to include
  from.
- `<script src=...>` to anything but `/static/...`
- `<link href=...>`, `<img src=...>`, `<form action=...>`, `<a href=...>` to any
  `http://`, `https://`, or `//` URL. The inside test `no_external_requests`
  fails the site on any of these; QA's `QA-ISO-02` catches them earlier, whether
  or not they would fire.
- Inline scripts that fetch, `XMLHttpRequest`, `<iframe>`, `<link rel="preconnect">`,
  and `data:` payloads over 4 KB.

## Context available to a template

| name | value |
|---|---|
| `site_name` | the site id |
| `query` | the `q` query parameter, or `""` |
| `<query id>` | a single row object if the query's `where` binds `id`; otherwise a list of rows |
| `<search id>` | a list of `{id, title, score}` for the `q` parameter, on routes with a `search` |

Rows are dictionaries keyed by column name. There is no `url_for`; write paths
literally, they are stable by construction.

## Selectors the agent will act on

Every interactive element (link, input, textarea, button, form) needs a stable
selector: an `id`, or failing that a `data-*` attribute. The env broker offers
elements to the agent by `#id` first, then by `[data-x="y"]`, and never by
position. An element with neither is invisible to the agent.

Form fields need a `name`, which is the form field the spec declares, and an `id`,
which is what the agent types into. The two may differ.

Results pages: each result gets `class="result-item"`; `search_returns` counts
those.

## Assets

Declare every asset in `spec.assets` with a `/static/...` path. Only declared
assets are served. Copy the files into `content/assets/` and reference them from
templates by the declared path. CSS is served as `text/css`; everything else as
`application/octet-stream`.
