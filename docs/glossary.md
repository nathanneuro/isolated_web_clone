# Glossary

One sentence per term, then where the term is defined or where the code that owns
it lives. Terms are grouped by the stage of the pipeline they belong to. If a term
in the specs or the code is not here, add it.

## Sides and links

| Term | Meaning | Owner |
|---|---|---|
| **outside** | The internet-connected side: scraper, reconstruction agents, QA, bundle-build, the dev signing key. | README, bundle-format-spec §8.1 |
| **inside** | The airgapped side: receiver, worker, go-live, registry, serving sandboxes, the agent zone. No LLM here reads content. | README invariants |
| **diode** | A hardware one-way link. Three exist: ingress (outside to inside), egress (inside to the dev reader), log (eval cluster to logging cluster). Nothing is acknowledged across any of them. | egress-metrics-spec §3, log-diode-spec §2 |
| **fake demo data diode** | A Python process copying files between two directories, standing in for the hardware so the demos run on one host. Provides the protocol and none of the isolation. | `tools/fake_demo_data_diode/` |
| **dev side** | The LAN-only host at the far end of the egress diode that runs the reader and the dashboard. | egress-metrics-spec §6 |
| **logging cluster** | The far end of the log diode. Logs are untrusted input; nothing leaves it uncleaned. | log-diode-spec |
| **red / black** | The plaintext side and the ciphertext side of an inline encryptor, on separate interfaces. Only black traffic ever touches a network nobody here controls. | remote-link-spec §1 |
| **black link** | A one-way link carried between sites by an encryptor pair, with the diodes left at the walls. A tunnel is a cable: it adds no permission. | remote-link-spec §3.1, `tools/black_link/` |
| **cell** | One fixed-size, authenticated black-link datagram. A cover cell carries nothing and is indistinguishable from one that does. | remote-link-spec §4.2, `tools/black_link/cell.py` |
| **wired terminal** | Display and keyboard in the room; the only way a human reads inside logs or exports bulk data. | physical-controls-spec |
| **zone** | An isolation boundary inside: the agent zone, the inference zone, the environment zone, the search engine. The reference code keeps zones in-process. | agent-sandbox-spec §3, §4 |

## The ingress unit

| Term | Meaning | Owner |
|---|---|---|
| **bundle** | The signed archive that crosses the ingress diode. Four types: `site`, `index_only`, `command`, `eval`. | bundle-format-spec §3, `schemas/manifest.schema.json` |
| **manifest** | The bundle's signed index: type, ids, sequence, file hashes, signer key id. | bundle-format-spec §4 |
| **structure** | Everything in a bundle that stays plaintext: the site spec, templates, test suite, population shape. Linted, schema-only, no free text. | bundle-format-spec §6 |
| **content** | Everything scraped or invented that an LLM must not read inside: seed rows, page text, content pools. Encrypted to the go-live key. | bundle-format-spec §5 |
| **slot** | A named place in the spec where content belongs, filled at build time by a handle. | bundle-format-spec §5, `tools/bundle_build/build.py` `find_slots` |
| **handle** | The BLAKE3 hash of an encrypted blob, standing in for the blob wherever structure refers to it. The worker sees handles, never blobs. | bundle-format-spec §5 |
| **site spec** | `spec/site.json`: tables, routes, queries, forms, search, interactions. The whole description of what the site does. | bundle-format-spec §6, `schemas/site.schema.json` |
| **test suite** | `tests/suite.json`: the checks go-live runs against the composed site, each named by id and answered by a code. | bundle-format-spec §7, `schemas/suite.schema.json` |
| **sequence** | The monotonic counter per signer that the receiver uses to refuse replayed bundles. | bundle-format-spec §4, `tools/receiver/` |
| **revision** | A new bundle for a site that already exists; supersedes the live one through the registry. | `tools/registry/` |
| **lint code** | A finding id from `schemas/lint-codes.toml` (for example `LINT-SCHEMA`, `XREF-01`), emitted outside before signing and again inside on receipt. | `tools/bundle_lint/` |
| **QA code** | A finding id from `schemas/qa-codes.toml` raised by the outside adversarial pass, such as `QA-VERB-01` for generated filler. | `skills/site-qa/` |
| **status code** | An integer from `schemas/status-codes.toml`: receiver verdicts, worker progress, go-live results, command outcomes. The only vocabulary that crosses back toward the worker or out the egress channel. | bundle-format-spec §8.3 |

## Inside: from archive to serving site

| Term | Meaning | Owner |
|---|---|---|
| **receiver** | Deterministic, no LLM. Checks layout, signature, sequence, hashes, lint. Accepts to an inbox or quarantines. | bundle-format-spec §8.2, `tools/receiver/` |
| **quarantine** | Where a rejected bundle or an unverified log record waits for a human. Nothing is partially processed. | `tools/receiver/`, log-diode-spec §5 |
| **worker** | The inside LLM agent. Reads structure and handles only, classifies the spec, composes an app, hands it to go-live, registers the result. | bundle-format-spec §8.4, `tools/worker/`, `skills/inside-worker/` |
| **classify** | The dry-run check that a spec uses only what the composer supports. A spec outside that set is `UNSUPPORTED`, never partially built. | `tools/compose_fastapi_sqlite_v1/classify.py` |
| **compose** | Turning a spec plus templates into a runnable app. Tier A is `compose_fastapi_sqlite_v1`. | `tools/compose_fastapi_sqlite_v1/` |
| **Tier A / Tier B** | Tier A: dynamic sites with forms and a database. Tier B: static sites (`compose-static-v1`, not written). | `skills/site-reconstruct/references/tiering.md` |
| **go-live** | Deterministic, no LLM. Unwraps the content key, decrypts into a sandbox, composes, runs the suite, returns codes and never plaintext. | bundle-format-spec §8.5, `tools/golive/` |
| **sandbox** | The serving copy of one site revision: decrypted content, composed app, spec. The only place plaintext content exists inside. In the reference code, a directory. | `tools/golive/` |
| **registry** | Hostname to live revision. Sites go live, are superseded, or retire here; the search engine and episode factory read from it. | `tools/registry/` |
| **hostname** | `<site-id>.internal`. Every site is reachable only at a name the registry holds. | `tools/brokers/env_broker.py` `CROSS_SITE` |
| **fake-web search** | The cross-site search engine, mounted over every live site's BM25 shard from the registry. Installed inside like model weights, never via the diode. | scale-and-storage-spec §3, `tools/search_engine/` |
| **shard** | A site's own BM25 index, shipped in the bundle and serving that site's search box. | `tools/compose_fastapi_sqlite_v1/compose.py` `Bm25Shard` |
| **command bundle** | A dev-signed instruction: start or stop a run, set a site live, retire, set the sequence floor, rotate a verify key. Parameters are enums and ids. | bundle-format-spec §9, `tools/command_executor/` |
| **run control** | The inside state machine a command drives: idle, running, paused, done, error. Reported as `run.state`; training code plugs in here. | `tools/command_executor/run_control.py` |

## The population

| Term | Meaning | Owner |
|---|---|---|
| **ambient population** | The synthetic users who post on a site whether or not an eval is running. Pipeline-signed, ships with the site. | synthetic-population-spec §2.1, §4.3, `schemas/population.schema.json` |
| **choreography** | Scripted actors who act at set steps of one eval question. Dev-signed, ships as an `eval` bundle, filed by question id. | synthetic-population-spec §2.2, §4.4, `schemas/choreography.schema.json` |
| **content pool** | A blob of rows the population draws its posts from. Content, so encrypted like everything else. | synthetic-population-spec §4.1 |
| **actor** | One scripted user in a choreography, bound to a `user_ref` from the site's seed. | `schemas/choreography.schema.json` |
| **driver** | The inside process that plays the population against an episode's database copy, deterministically, under a rate cap. | synthetic-population-spec §5, `tools/population/driver.py` |
| **eval intake** | Unseals an eval bundle, checks its choreography against the live site it names, files it. Waits if the site is not live. | `tools/eval_intake/` |
| **writer** | The column every mutation table carries, naming who wrote the row: `agent`, an actor id, a cohort id, or `golive`. Scoring credits only the agent. | `tools/compose_fastapi_sqlite_v1/compose.py` `WRITER_COLUMN` |

## The agent and its episode

| Term | Meaning | Owner |
|---|---|---|
| **agent zone** | Where the model under evaluation runs. It can reach exactly two endpoints, the brokers, and nothing else. | agent-sandbox-spec §3.3 |
| **action broker** | The agent's path to inference: messages in, a JSON action out, with the canary check on every reply. | `tools/brokers/action_broker.py` |
| **env broker** | The agent's path to the fake web: applies an action to the current site, returns a clamped observation, follows cross-site links through the registry. | `tools/brokers/env_broker.py` |
| **gate** | Held outside both brokers: sever authority, the canary token, schema-violation and write counters. The agent-facing surface never sees it. | `tools/brokers/gate.py` |
| **canary** | A token that must never appear in model output. Its appearance is an escape indicator and trips the watchdog. | agent-sandbox-spec §5.1 |
| **sever** | Cutting both brokers so the agent zone can reach nothing. The first halt step. | `tools/brokers/gate.py` |
| **watchdog** | Runs the detectors, moves CLEAN to HALTING to HALTED, and executes halt actions in order, continuing past any that fail. | agent-sandbox-spec §5.2, §6, `tools/watchdog/` |
| **detector** | One escape signal, D1 through D11 in the sandbox spec: canary, file integrity, state bypass, log patterns, liveness. | agent-sandbox-spec §5.1, `tools/watchdog/detectors.py` |
| **halt** | Freeze, sever, checkpoint, preserve. Happens before anyone is told. | agent-sandbox-spec §6 |
| **preserve** | Copying episode state aside at halt so the incident can be read at the terminal. | `tools/watchdog/watchdog.py` `ReferenceHaltActions` |
| **episode** | One question, one epoch: a fresh database copy per site touched, a home directory, and the drivers for those sites. | `tools/eval_harness/task.py` |
| **question** | An `EvalQuestion`: task text, starting site, gold state, milestones, minefields, step budget. | `tools/eval_harness/question.py` |
| **gold state** | The database condition that means the task was done, optionally on a site other than the start. | `tools/eval_harness/question.py` `GoldState` |
| **milestone** | A partial-credit state checked every step. | `tools/eval_harness/question.py` |
| **minefield** | A state the agent must not create; reaching it zeroes the reward. | `tools/eval_harness/question.py` |
| **collateral** | Rows the agent wrote outside the question's scope. Counted against it. | `tools/eval_harness/scorer.py` |
| **state-diff scorer** | Correct or incorrect from the database, credited only to rows the agent wrote. | `tools/eval_harness/scorer.py` |
| **reward scorer** | Zero to one from gold, milestones, minefields, and collateral. | `tools/eval_harness/scorer.py` |
| **site switch** | The env broker leaving one hostname for another the registry holds; the second site is materialised on arrival. | `tools/eval_harness/task.py` `MultiSiteEnvFactory` |

## Getting information out

| Term | Meaning | Owner |
|---|---|---|
| **egress channel** | The one automated path out: fixed-size frames of integers to the dev reader. | egress-metrics-spec |
| **frame** | One fixed-size egress record: metric id, value, timestamp. No strings, no variable length. | egress-metrics-spec §4 |
| **metric registry** | `schemas/metrics-registry.toml`: every metric id, its name, and its range. Compiled into sender and reader; a value outside the registry never leaves. | egress-metrics-spec §8 |
| **telemetry** | The inside socket components report their counters to; the sender frames what it collects. | `tools/egress/telemetry.py` |
| **reading store** | The dev-side record of frames the reader accepted. The dashboard reads nothing else. | `tools/egress/telemetry.py` `ReadingStore` |
| **log diode** | The one-way link from the eval cluster to the logging cluster. Trajectories and audit logs go this way, never the egress channel. | log-diode-spec |
| **log source** | The fixed registry of who may emit a log record; unknown sources are quarantined. | `tools/log_ingest/emitter.py` `LogSource` |
| **quarantine tier** | Where log records land before verification and sanitising; the promoted tier is what humans may read. | log-diode-spec §5, `tools/log_ingest/` |
| **cleaning** | The procedure for anything leaving the logging cluster; nothing leaves raw. | log-diode-spec §6 |
