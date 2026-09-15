# QA finding codes, and what each asks you to change

The registry is `schemas/qa-codes.toml`. A repair ticket is
`{failing_tests: [...], qa_findings: [...], lint_codes: [...]}`, codes only.
Address what the ticket names and nothing else; an unexplained change on a repair
pass is itself a finding (`QA-DIFF-01`).

| code | meaning | what to change |
|---|---|---|
| `QA-STRUCT-01` | bundle-lint reported a finding | fix the lint code listed in `lint_codes`; see `../../../schemas/lint-codes.toml` |
| `QA-STRUCT-02` | a `blob_ref` or `fixture_blob_ref` does not resolve to a file with a compatible role | point the slot at the right file under `content/`, or add the file |
| `QA-STRUCT-03` | a file under `content/` is referenced by no slot | delete it, or declare the slot that uses it |
| `QA-LEAK-01` | an identifier or route path is derived from site text | rename to schema vocabulary (`r_post`, not `r_bestvacuums`); parameterise the path |
| `QA-LEAK-02` | a string reads as an instruction | remove it from structure; if it is page text it belongs in the seed |
| `QA-VERB-01` | a seed row cannot be traced verbatim to the extract | delete generated or paraphrased rows; reload from `extract/text/` |
| `QA-VERB-02` | the seed's ordering column does not reproduce the original order | fix the ordering column values, or the query's `order_by` |
| `QA-AFF-01` | an interactive element from the explorer session is missing | add the element with a stable selector |
| `QA-AFF-02` | an element is present but does nothing and is not declared dead_end | wire it (route, form, mutation) or declare the interaction `dead_end` |
| `QA-AFF-03` | declared dead_end, but the explorer exercised it and the schema can express it | express it; see `spec-schema.md` for what the vocabulary covers |
| `QA-TASK-01` | a task possible on the original, within tier scope, is impossible on the replica | find the missing route, link, form, or search that blocks the path and add it |
| `QA-ISO-01` | an outbound request to something other than the replica or `/static/` was observed | remove the reference; see `template-subset.md` |
| `QA-ISO-02` | a template or asset contains an external fetch construct | remove it, whether or not it fired |
| `QA-FIX-01` | a fixture is missing, regenerated, or implausible for its test kind | take query fixtures from real titles, screenshots from `scrape/pages/`, inputs of the field's type |
| `QA-DIFF-01` | a change on a repair pass is not attributable to a code in the previous ticket | revert it |
| `QA-SUITE-01` | the suite does not cover every route, mutation, search, and isolation check | add the missing tests; see `test-kinds.md` |
| `QA-META-01` | no code expresses the finding | nothing for you to do; the registry gets a new code |

`build.json.flag`: 0 none, 1 schema_gap (you set this with a `schema_gaps` entry
when the vocabulary could not express something), 2 adversarial_content_suspicion
(you or QA set this; a human reviews the site).
