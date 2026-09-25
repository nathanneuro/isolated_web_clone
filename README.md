# Airgapped Web Environment for Agent Training

A reference design and support code for training and evaluating web agents against a local "fake internet" inside an airgapped environment, with one-way ingress and egress and no path for an agent, a model, or a scraped page to reach the public internet.

This repository contains the **harness, specs, prompts, and agent skill docs**. It does not contain scraped web data, and the scraper itself is left to the reader. Everything here runs end-to-end on the included synthetic example site.

## Why

Recent web-agent incidents have shared a shape: an agent with a browser, a model with a network path, and a log shipper with a token. This design removes all three. Scraped sites are rebuilt as functional-but-not-identical replicas outside, encrypted, pushed through a hardware data diode, and composed inside by a worker that never reads the content it deploys. The only automated path out is a fixed-schema numeric channel to a dev-owned reader. Humans read logs at a wired terminal in the room.

## Who this is for

Labs running evaluations against models capable enough that "the model broke out of the eval harness" is an incident report rather than a thought experiment. That is a short list today. It is a design premise of this document that the list gets longer, and that the time to have a published, reviewed architecture is before you need it rather than after.

It is sized accordingly: **hundreds of thousands of reconstructed sites**, from a crawl-and-reconstruction effort at serious cost, with per-episode environment resets cheap enough to be uninteresting. Several design calls invert at that scale — [`docs/scale-and-storage-spec.md`](docs/scale-and-storage-spec.md) is explicit about which ones and why, because a reader who mentally substitutes "a few dozen sites" will reasonably conclude parts of this are over-built.

If your evals run models that cannot meaningfully attempt an escape, this is more machinery than you need. Take the parts that are cheap anyway: the one-way ingress, the structure/content split, and never letting an LLM inside the trusted zone read scraped text.

The environment design itself (crawl → LLM-regenerated functional replicas → local search engine → RL) is described in `docs/design-plan.md` with a survey of prior work (InSTA, WebArena, Search-R1, AppWorld, τ-bench, ToolSandbox). This README covers the security architecture that wraps it.

## Architecture

```
OUTSIDE (internet-connected)                 │ diode │  INSIDE (airgapped)
                                             │  ──▶  │
scraper ─▶ explorer ─▶ site-reconstruct ─▶   │       │  receiver ─▶ inside-worker ─▶ go-live ─▶ serving
(private)  (private)   (cheap agents)        │       │  (no LLM)   (LLM, structure  (no LLM,    sandboxes
                            │                │       │              only)           decrypts)      │
                         site-qa             │       │                                         fake-web
                       (cheap agents)        │       │                                       search engine
                            │                │       │                                             │
                       bundle-build ─────────┼──────▶│                                        RL / eval runs
                       (encrypt, lint, sign) │       │                                             │
                                             │       │                                             ▼
dev commands (signed) ───────────────────────┼──────▶│                                       egress-sender
                                             │       │                                             │
                                             │  ◀──  │                                             │
dev dashboard ◀── egress-reader ◀────────────┼───────┼─────────────────────────────────────────────┘
(LAN only)        (fixed-schema numbers)     │ diode │
                                             │       │
                                             │       │  wired terminal (humans; logs, debugging, export)
                                             │       │
                                             │       │  agent zone: microVM ─▶ brokers ─▶ inference
                                             │       │              │                     + environment
                                             │       │         escape watchdog ─▶ halt run
```

## Invariants

Everything in the repo exists to hold one of these:

1. **Inside has no network path out** except the outbound diode to the dev reader.
2. **Everything entering is one-way and either signed or opaque.** Dev commands are signed and replay-protected. Site content is encrypted to a key only the go-live service holds.
3. **No LLM inside reads site content.** The worker composes from plaintext structure; content is decrypted only inside serving sandboxes by a deterministic service.
4. **No free text crosses the diode in either direction.** Specs and tests are schema-only and linted. Egress is integers from a fixed registry, enforced in hardware upstream of the link, not only by the sender's own code.
5. **Every LLM that reads scraped content is outside, and its output format has nowhere for an injection to live.**
6. **The agent being trained is sandboxed from the inference and environment zones, and an escape stops the run automatically** rather than waiting for someone to read a dashboard.

## Documents

| Document | What it specifies |
|----------|-------------------|
| [`docs/design-plan.md`](docs/design-plan.md) | The environment: source map of prior work, system architecture, reward design, validation, build order |
| [`docs/bundle-format-spec.md`](docs/bundle-format-spec.md) | The ingress unit: layout, signing, content encryption, site spec schema, test suite, component responsibilities, command bundles, status codes |
| [`docs/egress-metrics-spec.md`](docs/egress-metrics-spec.md) | The outbound channel: fixed-size frames, metric registry, sender, reader, bandwidth ceiling |
| [`docs/agent-sandbox-spec.md`](docs/agent-sandbox-spec.md) | Isolating the agent being trained from the inference and environment zones; escape detection and the automatic halt |
| [`docs/physical-controls-spec.md`](docs/physical-controls-spec.md) | The wired developer station, hardware enforcement of the egress filter, bulk export |
| [`docs/scale-and-storage-spec.md`](docs/scale-and-storage-spec.md) | What 10^5 sites changes: where site data lives, where a real database belongs, and why those are different places |
| [`docs/synthetic-population-spec.md`](docs/synthetic-population-spec.md) | Millions of synthetic users: ambient liveness from the pipeline, and per-question eval choreography from the researcher |
| [`docs/log-diode-spec.md`](docs/log-diode-spec.md) | The third one-way link: eval cluster to logging cluster. Why the agent has a motive to edit the record, and why nothing leaves the logging cluster uncleaned |
| [`docs/remote-link-spec.md`](docs/remote-link-spec.md) | Remote sites: carrying the one-way links across a WAN with red/black encryptors, what a remote developer gets, and what stays refused |
| [`docs/glossary.md`](docs/glossary.md) | Every term above in one sentence each, with the spec section or file that owns it |
| [`skills/site-reconstruct/SKILL.md`](skills/site-reconstruct/SKILL.md) | Outside agent: scrape → spec + templates + seed DB + tests |
| [`skills/site-qa/SKILL.md`](skills/site-qa/SKILL.md) | Outside agent: adversarial checks before encryption |
| [`skills/inside-worker/SKILL.md`](skills/inside-worker/SKILL.md) | Inside agent: verified bundle → deployment, structure only |

Read them in that order.

## Repository layout

```
docs/                     specs and design plan (above)
  glossary.md             one sentence per term, with its owner
skills/
  site-reconstruct/       outside agent: scrape -> spec + templates + seed DB + tests
  site-qa/                outside agent: adversarial checks before encryption
  inside-worker/          inside agent: verified bundle -> deployment, structure only
                          (each with the references/ files its SKILL.md names)
tools/
  bundle_lint/            spec, suite, population, and choreography linter; runs outside
                          before signing and inside on receipt
  bundle_build/           outside: encrypt, rewrite handles, lint, sign; site, command, and eval bundles
  fake_demo_data_diode/   SIMULATED diode for demos and tests; replace with hardware
  receiver/               inside: layout, signature, sequence, hashes, lint; dispatch
  worker/                 inside: sanity-check, classify, compose, go-live, register; codes only
  compose_fastapi_sqlite_v1/  Tier A deterministic generator, plus dry-run classification
  golive/                 inside: unwrap key, decrypt into sandbox, compose, run suite, codes;
                          unseals eval bundles too
  eval_intake/            inside: eval bundles -> choreographies checked against the live site
  registry/               inside control plane: live revision per hostname, supersede, retire
  command_executor/       inside: signed dev commands -> run control, registry, receiver trust
  brokers/                the agent zone's only two reachable endpoints; cross-site via the
                          registry and fake-web search; the zone control gate
  inference_zone/         inside: model weights; reachable only via the action broker
  eval_harness/           Inspect AI task, solver, state-diff and reward scorers, run counters;
                          single-site and registry-backed multi-site episode factories
  population/             ambient liveness + eval choreography; form_submit, vote, edit_own, delete_own
  watchdog/               escape watchdog: detectors D1-D5, D8-D10, liveness, one-way halt
  search_engine/          fake-web search across live sites, mounted from the registry;
                          installed inside like model weights, never via the diode
  recon_check/            outside: the reconstructor's local lint + deploy + suite harness
  egress/                 numeric channel: frame, registry, sender, reader, metrics socket
  log_ingest/             log diode: record writer, framing, sanitiser, quarantine tier
  black_link/             one-way link encryptor: carries a diode'd link between sites;
                          fixed cells, constant rate, no handshake
schemas/
  site.schema.json        site spec JSON Schema
  suite.schema.json       test suite JSON Schema
  manifest.schema.json    bundle manifest JSON Schema, including command params
  population.schema.json  ambient population spec
  choreography.schema.json  eval choreography
  metrics-registry.toml   egress metric registry (compiled into sender and reader)
  lint-codes.toml         bundle-lint finding codes
  qa-codes.toml           QA finding codes
  status-codes.toml       bundle status, test result, and command codes
example/
  synthetic_site/         a small invented forum board; generate_content.py builds its
                          seed DB, BM25 shard, and fixtures deterministically
scripts/
  run_demo.py             ingress pipeline end to end, plus a command and the metrics channel
  run_eval_demo.py        an eval with the watchdog and both outbound channels
  fetch_demo_models.py    fetch the two sub-1B demo models (runs OUTSIDE the airgap)
  demo_layout.py          writes LAYOUT.md into each run directory: which zone owns each entry
tests/                    one file per tool; `uv run pytest`
models/                   gitignored. Weights are provisioned physically, never by diode.
```

## Quick start (single machine, no diode)

Both scripts run both sides on one host with the diode replaced by a directory copy.
This exercises every component except the hardware.

```
uv run python example/synthetic_site/generate_content.py   # once: invent the example site
uv run python scripts/run_demo.py                          # ingress: build -> diode -> receiver
                                                           #   -> worker -> go-live -> registry,
                                                           #   a signed command, metrics back out
uv run python scripts/fetch_demo_models.py                 # once, OUTSIDE: two sub-1B models
uv run --group demo python scripts/run_eval_demo.py        # an eval through the brokers, with
                                                           #   the watchdog and both channels
```

The diode in both is [`tools/fake_demo_data_diode/`](tools/fake_demo_data_diode/),
and the name is literal. It is a Python process moving files between two directories
on one host; it provides **no isolation whatsoever** and exists so the pipeline can be
run and tested without buying hardware. What it does provide is the *protocol* a real
diode imposes: no acknowledgments, no back-channel, no retransmit requests,
fail-closed on malformed input, plus the drop and bit-flip failure modes a plain file
copy never produces. Components on both sides are written against those constraints,
so swapping in real one-way hardware changes nothing but the endpoints. Read the
package docstring before deploying anything.

Expected from `run_demo.py`: one bundle received, composed, passed go-live,
registered and indexed; a `start_run` applied; `sites.live = 1` and
`run.state = 1` on the dev-side store. Expected from `run_eval_demo.py`: the same
ingress, then two episodes on the multi-site factory with the site's shipped
population posting alongside the agent and scored as nobody's, trajectories
promoted on the logging side, and a canary trip that halts the run and flips
`sandbox.escape_indicator` on the dashboard.

Each run directory gets a `LAYOUT.md` saying which zone would own each entry in
a real deployment, since on one host they all sit side by side.

## What is deliberately not here

- **The scraper and explorer.** They touch the real internet and are the part most likely to need per-deployment judgement about robots, terms, and rate limits. `site-reconstruct` documents the input layout they must produce.
- **Any scraped content.** The example site is invented.
- **Training code.** The run loop is yours; `tools/command_executor` (`RunControl`) and `tools/egress` (`Telemetry`) are the interfaces it plugs into.
- **Physical controls.** No removable media inside, terminal is display-and-keyboard only, two-person export procedure, and the FPGA egress filter that makes the rate cap real rather than advisory. [`docs/physical-controls-spec.md`](docs/physical-controls-spec.md) specifies these; code cannot enforce them.
- **Diode hardware.** `tools/fake_demo_data_diode/` simulates the protocol so the pipeline runs on one machine. It is not a diode and does not pretend to be.
- **Encryptor hardware.** `tools/black_link/` is the protocol a red/black inline encryptor must speak to run through a diode. It is a Python process, not a HAIPE box, and [`docs/remote-link-spec.md`](docs/remote-link-spec.md) §5 says what the hardware has to add.

## Threat model in one paragraph

This design defends against the common leak paths: an agent with network access, a model reading injected content with authority, a log or telemetry pipeline that carries strings, unsigned or replayed instructions, and content tampering in transit. It bounds but does not eliminate low-bandwidth signalling from inside to the dev reader (about 34 bits/s, to a host with no internet path). It does not defend against a compromised terminal room, a compromised dev reader host, compromised diode firmware, or an outside pipeline that signs a malicious structure. Those are handled by physical controls, host hardening, vendor trust, and the outside QA loop respectively. It is a step up from current practice, not a claim of a sealed system.

## License

Apache-2.0. See [`LICENSE`](LICENSE).

## Status

v0.2. Every component the specs name has a reference implementation under `tools/`
with tests, except the ones listed under "What is deliberately not here". The
remaining gaps are known and are not the design:

- The zone split is in-process. The brokers are Python objects, not vsock endpoints,
  and the go-live sandbox is a directory, not a container or microVM.
- Detectors D6 and D7 are `LogPatternCounter` configurations over the audit log
  rather than named classes; everything else in the sandbox spec's table exists.
- Tier B (`compose-static-v1`) is not written. A Tier B bundle classifies
  UNSUPPORTED inside.
What goes through the diode is content and intent: site bundles, their ongoing
revisions, signed commands, and new or updated evals as dev-signed `eval` bundles. Infrastructure (the search engine, the brokers, the
watchdog, model weights) is installed inside before evals begin, by the wired
terminal and physical media, and is updated the same way.

Issues and pull requests welcome, especially findings against the invariants above.
