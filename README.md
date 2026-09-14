# Airgapped Web Environment for Agent Training

A reference design and support code for training and evaluating web agents against a local "fake internet" inside an airgapped environment, with one-way ingress and egress and no path for an agent, a model, or a scraped page to reach the public internet.

This repository contains the **harness, specs, prompts, and agent skill docs**. It does not contain scraped web data, and the scraper itself is left to the reader. Everything here runs end-to-end on the included synthetic example site.

## Why

Recent web-agent incidents have shared a shape: an agent with a browser, a model with a network path, and a log shipper with a token. This design removes all three. Scraped sites are rebuilt as functional-but-not-identical replicas outside, encrypted, pushed through a hardware data diode, and composed inside by a worker that never reads the content it deploys. The only automated path out is a fixed-schema numeric channel to a dev-owned reader. Humans read logs at a wired terminal in the room.

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
```

## Invariants

Everything in the repo exists to hold one of these:

1. **Inside has no network path out** except the outbound diode to the dev reader.
2. **Everything entering is one-way and either signed or opaque.** Dev commands are signed and replay-protected. Site content is encrypted to a key only the go-live service holds.
3. **No LLM inside reads site content.** The worker composes from plaintext structure; content is decrypted only inside serving sandboxes by a deterministic service.
4. **No free text crosses the diode in either direction.** Specs and tests are schema-only and linted. Egress is integers from a fixed registry.
5. **Every LLM that reads scraped content is outside, and its output format has nowhere for an injection to live.**

## Documents

| Document | What it specifies |
|----------|-------------------|
| [`docs/design-plan.md`](docs/design-plan.md) | The environment: source map of prior work, system architecture, reward design, validation, build order |
| [`docs/bundle-format-spec.md`](docs/bundle-format-spec.md) | The ingress unit: layout, signing, content encryption, site spec schema, test suite, component responsibilities, command bundles, status codes |
| [`docs/egress-metrics-spec.md`](docs/egress-metrics-spec.md) | The outbound channel: fixed-size frames, metric registry, sender, reader, bandwidth ceiling |
| [`skills/site-reconstruct/SKILL.md`](skills/site-reconstruct/SKILL.md) | Outside agent: scrape → spec + templates + seed DB + tests |
| [`skills/site-qa/SKILL.md`](skills/site-qa/SKILL.md) | Outside agent: adversarial checks before encryption |
| [`skills/inside-worker/SKILL.md`](skills/inside-worker/SKILL.md) | Inside agent: verified bundle → deployment, structure only |

Read them in that order.

## Repository layout

```
docs/                     specs and design plan (above)
skills/
  site-reconstruct/       SKILL.md + references/ (spec-schema, test-kinds, template-subset, qa-codes, tiering)
  site-qa/                SKILL.md + references/ (qa-codes, browser-harness)
  inside-worker/          SKILL.md + references/ (patterns, retry-rules, worker-cli)
tools/
  bundle-lint/            spec/test linter; runs outside before signing and inside on receipt
  bundle-build/           assemble, encrypt, lint, sign
  recon-check/            local deploy + test harness for outside agents (unencrypted, full logs)
  compose-fastapi-sqlite-v1/   Tier A deterministic generator
  compose-static-v1/           Tier B deterministic generator
  receiver/               inside: verify layout, signature, sequence, hashes, lint; dispatch
  golive/                 inside: unwrap key, decrypt into sandbox, run suite, return codes
  worker-cli/             inside: inbox, load, golive, register, retire
  egress-sender/          inside: metrics socket → frames → diode
  egress-reader/          outside: frames → local time-series store; includes fuzz harness
  command-executor/       inside: signed command bundles → run loop
schemas/
  site.schema.json        site spec JSON Schema
  suite.schema.json       test suite JSON Schema
  manifest.schema.json    bundle manifest JSON Schema
  metrics-registry.toml   egress metric registry (compiled into sender and reader)
  qa-codes.toml           QA finding codes
  status-codes.toml       bundle status and subcode registry
example/
  synthetic-site/         a small invented forum site: scrape/, explorer/, extract/ as the pipeline expects
  walkthrough.md          run the whole pipeline on it, both sides, on one machine
```

## Quick start (single machine, no diode)

The walkthrough runs both sides on one host with the diode replaced by a directory copy. This exercises every component except the hardware.

```
make example            # reconstruct + qa + build the synthetic site → ./out/bundles/
make inside-up          # start receiver, go-live, worker, search engine, egress-sender in containers
make push               # copy bundles into the receiver's inbox (stands in for the diode)
make status             # tail egress-reader output
```

Expected: one bundle received, composed, passed go-live, registered; `sites.live = 1` on the dashboard.

## What is deliberately not here

- **The scraper and explorer.** They touch the real internet and are the part most likely to need per-deployment judgement about robots, terms, and rate limits. `site-reconstruct` documents the input layout they must produce.
- **Any scraped content.** The example site is invented.
- **Training code.** The run loop is yours; `command-executor` and `egress-sender` document the interfaces it plugs into.
- **Physical controls.** No removable media inside, terminal is display-and-keyboard only, two-person export procedure. The specs state these; they cannot enforce them.

## Threat model in one paragraph

This design defends against the common leak paths: an agent with network access, a model reading injected content with authority, a log or telemetry pipeline that carries strings, unsigned or replayed instructions, and content tampering in transit. It bounds but does not eliminate low-bandwidth signalling from inside to the dev reader (about 34 bits/s, to a host with no internet path). It does not defend against a compromised terminal room, a compromised dev reader host, compromised diode firmware, or an outside pipeline that signs a malicious structure. Those are handled by physical controls, host hardening, vendor trust, and the outside QA loop respectively. It is a step up from current practice, not a claim of a sealed system.

## License

Apache-2.0. See [`LICENSE`](LICENSE).

## Status

Draft specs, v0.1. Reference files under `skills/*/references/` and the tools under `tools/` are the next pieces to land. Issues and pull requests welcome, especially findings against the invariants above.
