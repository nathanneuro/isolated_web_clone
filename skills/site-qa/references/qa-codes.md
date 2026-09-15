# QA finding codes

The registry is `schemas/qa-codes.toml`, and it is the only vocabulary a ticket
may use. The reconstructor's view of the same codes, with what each asks them to
change, is `../../site-reconstruct/references/qa-codes.md`. This page is the
QA-side reading: which check produces which code, and how sure you must be.

| check | code | file it when |
|---|---|---|
| Q1 | `QA-STRUCT-01` | `bundle-lint` returned anything; copy its codes into `lint_codes` |
| Q1 | `QA-STRUCT-02` | a slot points at a file that is missing or has the wrong role per `build.json` |
| Q1 | `QA-STRUCT-03` | a file under `content/` is referenced by no slot |
| Q2 | `QA-LEAK-01` | any string in `spec/` or `tests/` came from the site rather than the schema vocabulary; add the key path to `locations` |
| Q2 | `QA-LEAK-02` | any string reads as an instruction, whatever its length; this also sets `flags: [2]` |
| Q3 | `QA-VERB-01` | a sampled seed row has no exact match in `queue/extract/text/` after whitespace normalisation |
| Q3 | `QA-VERB-02` | the sampled rows' ordering column does not reproduce the original listing order |
| Q4 | `QA-AFF-01` | an explorer-session element has no counterpart in the replica |
| Q4 | `QA-AFF-02` | the counterpart exists, does nothing, and the interaction is not declared `dead_end` |
| Q4 | `QA-AFF-03` | declared `dead_end`, the explorer exercised it successfully, and `spec-schema.md` can express it |
| Q5 | `QA-TASK-01` | one of your three task walks fails on the replica for a task in tier scope |
| Q6 | `QA-ISO-01` | the harness observed a request to any host other than the replica or `/static/`; sets `flags: [2]` |
| Q6 | `QA-ISO-02` | a template or asset contains an external fetch construct, fired or not; sets `flags: [2]` |
| Q7 | `QA-FIX-01` | a fixture is missing, regenerated, or implausible for its kind |
| Q8 | `QA-DIFF-01` | on a resubmission, a changed file is not attributable to a code in the previous ticket |
| Q9 | `QA-SUITE-01` | the suite lacks route_ok per route, form_persists per mutation, search_returns per search, or no_external_requests per route |
| any | `QA-META-01` | the finding is real and no code expresses it; file it beside the closest real code |

## Certainty

A ticket bounces a package. Most codes are mechanical and you should be certain.
Two are judgement calls, and the rule for both is the same: cleverness is
evidence. `QA-LEAK-02` when a string would make sense as a directive to a model
that read it. `QA-TASK-01` when a person could do the task on the original and
the tier permits it, even if the reconstructor's own tests pass.

## Flags on approval

Approving is not the end of your judgement. A site whose content is unusually
well-suited to slipping through structure checks gets `flags: [2]` on approval, and
a human looks at it before it ships.
