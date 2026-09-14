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
- Crypto: Ed25519 (signing), X25519 sealed box (key wrap), XChaCha20-Poly1305 (blobs),
  BLAKE3 (hashes). Use `pynacl` + `blake3`; do not hand-roll.
- Parsers that touch untrusted bytes (receiver, egress-reader) get a fuzz harness in
  CI and no deserialization library.

## Status

v0.1 draft. Specs are written; tools are being implemented bottom-up:
schemas → bundle-lint → bundle-build → receiver → go-live → worker → egress.
