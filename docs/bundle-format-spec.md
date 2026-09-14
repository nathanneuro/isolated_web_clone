# Airgapped Web Environment — Bundle Format Specification

**Version:** 0.1 (draft)
**Date:** 2026-09-14
**Status:** Design proposal, pre-implementation

---

## 1. Purpose

A *bundle* is the unit of transfer across the ingress data diode. Everything that enters the airgapped environment — site replicas, search index shards, dev commands — is a bundle. This document specifies the on-disk format, the split between plaintext structure and encrypted content, the signing and encryption envelope, and the responsibilities of each component that touches a bundle.

The format is designed around one invariant:

> **The inside worker agent composes deployments from structure it can read and content it cannot.** Content is decrypted only at go-live, by a deterministic service with no language model in it, into serving containers the worker does not inspect.

This removes the worker as a prompt-injection surface (it never reads scraped text) and bounds the damage from a misbehaving worker to "a site fails to deploy."

---

## 2. Scope and threat model

**In scope.** The bundle format defends against:

- Scraped content carrying instructions that reach an LLM with authority inside the airgap.
- Unsigned or replayed instructions being treated as dev commands.
- Tampered or corrupted content being served without detection.
- Content leaking into the plaintext structure via "example values," free-text notes, or test fixtures.

**Out of scope.** The format does not defend against a compromised terminal room, a compromised dev reader host, or a compromised outside pipeline that signs a malicious *structure*. Those are handled by physical controls and by the outside QA loop respectively. The format is one layer, not the whole system.

---

## 3. Bundle layout

A bundle is a tar archive (uncompressed at the top level; individual blobs may be compressed before encryption) with a fixed layout. The diode-side receiver rejects any archive containing paths outside this layout.

```
<bundle_id>.bundle/
├── manifest.json            # plaintext, signed (§4)
├── manifest.sig             # Ed25519 signature over manifest.json bytes
├── spec/
│   └── site.json            # plaintext structure (§6)
├── tests/
│   └── suite.json           # plaintext test structure (§7)
├── content/
│   ├── <blake3>.blob        # encrypted content blobs (§5)
│   └── ...
└── index/
    ├── <blake3>.shard       # encrypted search index shards
    └── ...
```

For `type: "command"` bundles (§9), `spec/`, `tests/`, `content/`, and `index/` are absent.

---

## 4. Manifest and signing

`manifest.json` is the root of trust for the bundle. It is the only file the receiver reads before verifying a signature.

```json
{
  "format_version": "0.1",
  "bundle_id": "site-000417-r3",
  "type": "site",
  "sequence": 8821,
  "created_at": "2026-09-14T18:22:07Z",
  "signer_key_id": "pipeline-2026q3",
  "site_id": "site-000417",
  "revision": 3,
  "supersedes": "site-000417-r2",
  "tier": "A",
  "content_key_wrapped": {
    "recipient_key_id": "golive-2026q3",
    "algorithm": "x25519-xsalsa20-poly1305",
    "ciphertext_b64": "..."
  },
  "files": [
    { "path": "spec/site.json", "blake3": "…", "bytes": 18422 },
    { "path": "tests/suite.json", "blake3": "…", "bytes": 6110 },
    { "path": "content/a3f9….blob", "blake3": "…", "bytes": 2201933, "role": "page_text" },
    { "path": "content/77c1….blob", "blake3": "…", "bytes": 940211, "role": "db_seed" },
    { "path": "index/0c4e….shard", "blake3": "…", "bytes": 33019441, "role": "bm25_shard" }
  ]
}
```

**Rules.**

1. `manifest.sig` is an Ed25519 signature over the exact bytes of `manifest.json`. The receiver holds only the *verification* key. Signing keys never enter the airgap.
2. `sequence` is a monotonic counter per `signer_key_id`. The receiver persists the highest sequence seen and rejects any bundle with `sequence <= last_seen`. This kills replay. Gaps are permitted (a bundle may be dropped outside) but logged.
3. Every file in the archive must appear in `files[]` with a matching BLAKE3 hash and byte length. Extra files → reject. Missing files → reject. Hash mismatch → reject.
4. `type` is one of `site`, `index_only`, `command`. The receiver dispatches on this field and nothing else.
5. `supersedes` allows a revision to replace an earlier one. The worker treats revisions atomically: the old deployment stays live until the new one passes go-live.
6. Unsigned bytes are data. The receiver never interprets anything outside a verified manifest as an instruction. This rule is enforced structurally: the command parser (§9) only runs on `type: "command"` bundles with a valid signature.

---

## 5. Content encryption

**Key hierarchy.**

- Each bundle has a fresh random 256-bit *content key*.
- Every blob under `content/` and `index/` is encrypted with the content key using XChaCha20-Poly1305 with a per-blob random nonce prepended to the ciphertext. The AAD is the blob's `role` string from the manifest, so a blob cannot be swapped into a different role even with a valid ciphertext.
- The content key is wrapped (sealed-box) to the go-live service's X25519 public key and stored in the manifest. Only the go-live service holds the corresponding private key, on the inside, in a process the worker cannot read from.
- Blob filenames are the BLAKE3 hash of the *ciphertext*, so the worker can verify integrity and reference blobs by handle without decrypting.

**Consequences.**

- The worker can verify every byte of a bundle and can reason about blob sizes, counts, and roles. It cannot read a single word of scraped text.
- Losing the go-live private key makes existing bundles unrecoverable inside. Key rotation is handled by re-wrapping outside and shipping a new revision; there is no inside re-wrap path by design.
- The content key is not a secret worth protecting against exfiltration (the underlying corpus is scrapeable), but it *is* the thing that keeps content away from the worker, so it must never be logged, never be written to a path the worker can read, and never be returned in any result code.

---

## 6. Site spec (`spec/site.json`)

The site spec is plaintext structure describing *what to build*, never *what it contains*. It is the worker's primary input.

```json
{
  "site_id": "site-000417",
  "tier": "A",
  "framework": "fastapi-sqlite-v1",
  "hostname": "site-000417.internal",
  "routes": [
    { "id": "r_home", "path": "/", "method": "GET", "template": "t_home", "queries": ["q_recent_posts"] },
    { "id": "r_search", "path": "/search", "method": "GET", "template": "t_results", "search": "s_main" },
    { "id": "r_post", "path": "/post/{post_id}", "method": "GET", "template": "t_post", "queries": ["q_post_by_id"] },
    { "id": "r_comment", "path": "/post/{post_id}/comment", "method": "POST", "form": "f_comment", "mutation": "m_insert_comment" }
  ],
  "templates": [
    { "id": "t_home", "blob_ref": "content/b81e….blob", "engine": "jinja2" }
  ],
  "db": {
    "tables": [
      { "name": "posts", "columns": [
        { "name": "id", "type": "integer", "pk": true },
        { "name": "title", "type": "text" },
        { "name": "body", "type": "text" },
        { "name": "created_at", "type": "timestamp" }
      ]},
      { "name": "comments", "columns": [
        { "name": "id", "type": "integer", "pk": true },
        { "name": "post_id", "type": "integer", "fk": "posts.id" },
        { "name": "body", "type": "text" },
        { "name": "author", "type": "text" }
      ]}
    ],
    "seed_blob_ref": "content/77c1….blob",
    "seed_format": "sqlite-dump-v1"
  },
  "queries": [
    { "id": "q_recent_posts", "table": "posts", "order_by": "created_at desc", "limit": 20 },
    { "id": "q_post_by_id", "table": "posts", "where": { "id": "{post_id}" } }
  ],
  "forms": [
    { "id": "f_comment", "fields": [
      { "name": "body", "type": "textarea", "required": true },
      { "name": "author", "type": "text", "required": false }
    ]}
  ],
  "mutations": [
    { "id": "m_insert_comment", "table": "comments", "op": "insert", "from_form": "f_comment", "bind": { "post_id": "{post_id}" } }
  ],
  "search": [
    { "id": "s_main", "kind": "bm25", "tables": ["posts"], "fields": ["title", "body"], "shard_refs": ["index/0c4e….shard"] }
  ],
  "interactions": {
    "auth": "none",
    "payments": "dead_end",
    "realtime": "none"
  },
  "assets": [
    { "path": "/static/style.css", "blob_ref": "content/9d02….blob" }
  ]
}
```

**Structure/content rule.** The following are *structure* and may appear in the spec: identifiers, route paths, column names and types, form field names and types, query shapes, template engine names, blob handles, enum values from this document. The following are *content* and must never appear in the spec: page text, titles, sample rows, example values, author names, URLs to external sites, free-text descriptions or notes of any kind.

The spec has **no free-text fields**. If the outside reconstruction agent needs to communicate something to the inside worker that does not fit the schema, that is a schema gap to be fixed outside, not a note to be passed inside.

**Blob handles are assigned at build time, not by the reconstructor.** A `blob_ref` is the BLAKE3 hash of a blob's *ciphertext* (§5), which does not exist until `bundle-build` encrypts. The reconstruction agent therefore authors slots with logical paths (`content/fixtures.json`, `content/assets/style.css`) and `bundle-build` rewrites every `blob_ref`, `seed_blob_ref`, `shard_refs` entry, and `fixture_blob_ref` to its content-addressed form as it encrypts, immediately before linting and signing. A rewrite that leaves any logical path unresolved is a build failure, not a lint finding.

Consequently the reference rules below can only be checked against a manifest. `bundle-lint` invoked without one — as `recon-check` does on a package that has not been built yet — skips them rather than approximating them. The reconstructor's refs are validated by the rewrite succeeding.

**Linting.** `bundle-lint` runs outside before signing and again on the receiver. It rejects a spec if:

- any string value exceeds 64 characters, except `blob_ref` and `path` fields (which are validated against their own patterns);
- any string value contains whitespace-separated runs of more than 4 words;
- any key is not in the schema's allowed set;
- any `blob_ref` does not resolve to a manifest entry with a compatible `role`;
- the framework value is not in the allowed set (`fastapi-sqlite-v1` for Tier A; `static-v1` for Tier B).

The lint rules are deliberately crude. They are a tripwire against the pipeline drifting toward "just put the description in a string," not a content classifier.

---

## 7. Test suite (`tests/suite.json`)

Tests are how the go-live service decides a deployment is correct, and how the worker learns whether it succeeded. Test *structure* is plaintext; any expected *value* that is content lives in an encrypted fixture.

```json
{
  "suite_id": "site-000417-r3-tests",
  "runner": "playwright-v1",
  "fixture_blob_ref": "content/e44a….blob",
  "tests": [
    { "id": "T001", "kind": "route_ok", "route": "r_home", "expect_status": 200 },
    { "id": "T002", "kind": "links_resolve", "route": "r_home", "min_internal_links": 5 },
    { "id": "T003", "kind": "search_returns", "search": "s_main", "query_fixture": "fx_q1", "expect_min_results": 3, "expect_contains_fixture": "fx_q1_doc" },
    { "id": "T004", "kind": "form_persists", "form": "f_comment", "route": "r_comment", "path_params_fixture": "fx_post_1", "input_fixture": "fx_comment_1", "verify_query": "q_comments_for_post", "expect_row_count_delta": 1 },
    { "id": "T005", "kind": "render_diff", "route": "r_home", "reference_fixture": "fx_home_png", "max_distance": 0.35 },
    { "id": "T006", "kind": "no_external_requests", "route": "r_home" }
  ]
}
```

**Rules.**

- Test `kind` is an enum implemented by the runner. New kinds require a runner release, not a schema change.
- Anything a test needs to *type into* or *compare against* is a fixture key. The fixture blob is encrypted like all other content and decrypted only inside the go-live sandbox.
- `no_external_requests` is mandatory for every Tier A site. The serving container has no network route out regardless, but the test catches templates that would try.
- Results are returned to the worker as `{test_id, result_code}` pairs only (§8.3). Never output, never diffs, never screenshots.

---

## 8. Component responsibilities

### 8.1 Outside pipeline (public code, private data)

1. Scrape and explore (private).
2. Reconstruct with cheap agents into a spec + templates + seed DB + tests.
3. QA loop until the suite passes locally.
4. Build the search index shards.
5. `bundle-build`: assemble layout, encrypt content and index, lint spec, write manifest, sign.
6. Push through the diode.

### 8.2 Receiver (inside, deterministic, no LLM)

1. Verify archive layout.
2. Verify `manifest.sig` against the pinned verification key.
3. Check `sequence` against persisted high-water mark; reject replays.
4. Verify every file hash and length.
5. Run `bundle-lint` on `spec/` and `tests/`.
6. Dispatch: `site` / `index_only` → worker inbox; `command` → command executor (§9).
7. Emit receipt code (§8.3) to the egress channel.

A bundle that fails any step is quarantined, never partially processed.

### 8.3 Result and receipt codes

All outbound status is a small integer from a fixed registry. No strings.

| Code | Meaning |
|------|---------|
| 0 | OK |
| 10 | Reject: layout |
| 11 | Reject: signature |
| 12 | Reject: replay |
| 13 | Reject: hash mismatch |
| 14 | Reject: lint |
| 20 | Worker: composed, awaiting go-live |
| 21 | Worker: compose failed, schema unsupported |
| 30 | Go-live: pass |
| 31 | Go-live: test fail (followed by per-test codes) |
| 32 | Go-live: decrypt fail |
| 33 | Go-live: timeout |
| 40 | Live |
| 41 | Retired (superseded) |

Per-test result codes: 0 pass, 1 fail, 2 skipped, 3 runner error.

### 8.4 Worker agent (inside, LLM-powered, reads structure only)

Input: verified bundle in inbox. The worker's filesystem view excludes `content/` and `index/` blob bodies; it sees the manifest entries for them (hash, size, role) and nothing else.

1. Parse `spec/site.json`.
2. Compose a deployment: generate the FastAPI app from the spec using the fixed framework's code generator (deterministic; the LLM's job is choosing among supported patterns and handling the long-tail cases the generator flags, not writing arbitrary code).
3. Register blob handles with the deployment: "this template slot receives blob X," "this DB is seeded from blob Y," "this search endpoint mounts shard Z."
4. Submit a go-live request: `{bundle_id, deployment_dir}`.
5. Receive `{status_code, [test_id, result_code]...}`.
6. On pass: mark live, register hostname with the inside DNS and the fake-web search engine's site list, retire superseded revision.
7. On fail: the worker may retry composition up to N times using *only* the test IDs and codes as signal. It never receives decrypted output. If it cannot pass, it marks the bundle failed (code 21 or 31) and moves on. Failed bundles are triaged by a human at the wired terminal.

The worker has no network access, no write access to the go-live service's key material, and no read access to live serving containers.

### 8.5 Go-live service (inside, deterministic, no LLM)

1. Receive `{bundle_id, deployment_dir}` from the worker.
2. Unwrap the content key with the go-live private key.
3. Create an isolated serving sandbox (container or Firecracker VM) with no network route except to the inside DNS and the fake-web search engine.
4. Decrypt blobs directly into the sandbox's filesystem. Plaintext never touches a path outside the sandbox.
5. Decrypt the test fixture into the runner's memory.
6. Run the suite.
7. Return codes to the worker. Destroy the fixture plaintext. On pass, the sandbox becomes the live deployment; on fail, destroy it.

The service is small enough to be reviewed in full and fuzzed against malformed bundles. It should have no dependencies beyond the crypto library, the container runtime, and the test runner.

---

## 9. Command bundles

Dev instructions use the same envelope with `type: "command"`. The manifest carries the command inline; there is no content section.

```json
{
  "format_version": "0.1",
  "bundle_id": "cmd-2026-09-14-0031",
  "type": "command",
  "sequence": 8822,
  "created_at": "2026-09-14T18:40:00Z",
  "signer_key_id": "dev-nathan-2026q3",
  "command": {
    "op": "start_run",
    "run_id": "eval-webarena-transfer-07",
    "config_ref": "runconfig-v12",
    "params": { "seed": 1337, "episodes": 2000, "sites": ["site-000417", "site-000418"] }
  },
  "files": []
}
```

**Rules.**

- `op` is an enum: `start_run`, `stop_run`, `set_live`, `retire`, `rotate_verification_key`, `set_sequence_floor`. New ops require a command-executor release.
- `config_ref` points to a run config that already exists inside (shipped earlier as a `site`-style bundle with `role: "run_config"`, or baked into the image). Commands never carry code, scripts, or config bodies inline.
- `params` is validated against a per-op schema with the same lint rules as the site spec (no long strings, no free text).
- Dev signing keys are distinct from pipeline signing keys. A `site` bundle signed with a dev key is rejected, and vice versa.
- `rotate_verification_key` must be signed by a designated rotation key held offline; it is the one op that changes what the receiver trusts.

---

## 10. Versioning

- `format_version` is checked exactly by the receiver. Receivers ship with a list of accepted versions.
- Breaking changes to the spec or test schemas bump the major version. The framework identifier (`fastapi-sqlite-v1`) versions the inside code generator independently.
- A site revision is immutable once signed. Fixes are new revisions with `supersedes` set.

---

## 11. Open questions

1. **Index rebuild on state change.** Tier A sites mutate their DB during episodes. Shipping a pre-built index means search reflects the seed state, not the live state. Options: accept it (search is for discovery, not for verifying the agent's own edits); or run a small inside indexer in the no-LLM zone that reindexes from the decrypted DB per episode reset. Leaning toward the first for v0.1.
2. **Long-tail composition.** How much of Tier B (static) can be handled by a fully deterministic composer with no LLM in the loop? If most of it, the worker's LLM becomes an exception handler rather than the main path, which is the direction to push.
3. **Fixture granularity.** A single fixture blob per suite is simple but means one decrypt for the whole suite. Fine for now; revisit if suites get large.
4. **Blob deduplication across bundles.** Shared assets (a CSS framework used by many sites) will be re-encrypted per bundle under different keys. Wasteful but keeps the key hierarchy flat. A shared-asset bundle type is a possible later addition.
5. **Sequence floor recovery.** If the receiver's persisted high-water mark is lost, every bundle in flight replays as new. `set_sequence_floor` exists for this, but the procedure for exercising it safely needs writing.
