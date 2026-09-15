# CLAUDE.md

Reference design + support code for an airgapped web-agent training environment.
Read `README.md` first, then `docs/bundle-format-spec.md`, then `docs/egress-metrics-spec.md`.

## Hard rules for this repo

These are not style preferences; they are the invariants the whole design exists to hold.
Code that breaks one of them is a bug even if the tests pass.

1. **No scraped web data in the repo.** `example/synthetic-site/` is invented. Never
   commit anything crawled. The scraper and explorer are deliberately absent.
2. **No free-text fields in any schema that crosses the diode.** Site specs, test
   suites, command params, and egress frames carry identifiers, enums, and numbers.
   If something doesn't fit the schema, the schema is wrong — do not add a `notes`
   string.
3. **Nothing inside the airgap that reads content may be an LLM.** The worker sees
   structure and blob handles; go-live decrypts and never returns plaintext, only
   codes.
4. **Egress is integers from a fixed registry.** No strings, no stack traces, no
   variable-length frames on the outbound channel.
5. **Fail closed.** Reject/quarantine on any verification failure. Never partially
   process a bundle.

## Conventions

- `uv run python`, not `python`. Python >=3.13.
- Tools live under `tools/<name>/` as importable packages with a `__main__.py` CLI.
- Tests under `tests/`, mirroring `tools/`. `uv run pytest`.
- Outputs (bundles, logs, run artifacts) go to `outputs/run_<ts>_<slug>/`, never a
  global dump directory. `out/` and `outputs/` are gitignored.
- **Faker is for invented data only.** Use it in `example/synthetic-site/` and in test
  fixtures. Never use it, or any generator, to produce seed DB rows in the real
  pipeline: design-plan §3.2.2 requires scraped text verbatim, and `site-qa` Q3
  (`QA-VERB-01`) exists to catch generated filler. A row that cannot be traced to a
  source page is a defect.
- Property-based tests and parser fuzzing use `hypothesis`. The specs require fuzz
  harnesses for go-live (bundle-format-spec §8.5) and the egress reader.
- Crypto: Ed25519 (signing), X25519 sealed box (key wrap), XChaCha20-Poly1305 (blobs),
  BLAKE3 (hashes). Use `pynacl` + `blake3`; do not hand-roll.
- Parsers that touch untrusted bytes (receiver, egress-reader) get a fuzz harness in
  CI and no deserialization library.

## Status

v0.2. Every spec'd component has a reference implementation under `tools/` with
tests in `tests/`. See the README's Status section for what is deliberately
reference-grade (in-process zones, a directory sandbox, no Tier B generator). When adding a component: register its metrics in
`schemas/metrics-registry.toml` first, give it `as_metrics()`, and attach it to
`Telemetry` in both demo scripts.
