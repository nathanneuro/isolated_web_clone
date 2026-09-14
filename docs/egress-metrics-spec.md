# Airgapped Web Environment — Egress Metrics Channel

**Version:** 0.1 (draft)
**Date:** 2026-09-14
**Companion to:** Bundle Format Specification v0.1

---

## 1. Purpose

The egress channel is the only automated path from inside the airgap to anywhere else. It exists so developers can glance at run health when they are not at the wired terminal: is the run alive, is the loss moving, did the last bundle go live. It is not a logging system, not a debugging tool, and not a data export path. Anything that needs more than a glance happens at the terminal.

The design principle is that the channel carries **numbers from a fixed registry at a fixed rate**, and the thing that reads them on the far side is small enough to review in full and fuzz to exhaustion.

## 2. Scope and threat model

**In scope.**

- Execution on the reader host: the receiver parses a fixed binary schema with a hand-written parser. There is no deserializer, no string handling, no dynamic dispatch. A malformed frame is dropped.
- Accidental leakage: the channel cannot carry strings, so log lines, prompts, transcripts, error messages, and file paths cannot leave by this route even if something inside tries to send them.
- Schema drift: the registry of metric IDs is versioned and pinned on both sides; an unknown ID is dropped and counted.

**Out of scope, and stated as such.**

- Covert signalling to the developers' own reader is not a leak; the reader is dev-owned and offline from the public internet. The channel bounds the bandwidth of such signalling (§7) but does not attempt to prevent it.
- Compromise of the reader host itself is in the same class as compromise of the terminal room: handled by physical and host controls, not by this spec.

## 3. Physical path

```
inside sender ──▶ outbound data diode ──▶ reader host ──▶ dev dashboard
                  (one-way, hardware)     (no inbound      (LAN only,
                                           network path     no internet)
                                           except the
                                           diode)
```

The reader host has exactly two network interfaces: the diode receive side and a LAN interface to the dev dashboard. It has no route to the public internet. The dashboard is Grafana or similar, reading from the reader's local store.

## 4. Frame format

Every transmission is one fixed-size frame. No variable-length fields, no framing headers that carry length, no compression.

```
offset  size  field
0       4     magic            0x41474D31 ("AGM1")
4       2     schema_version   uint16
6       2     registry_version uint16
8       8     frame_sequence   uint64, monotonic per sender
16      8     timestamp        uint64, seconds since inside epoch
24      2     run_id           uint16, from run registry (0 = system)
26      2     metric_count     uint16, 0..MAX_METRICS
28      n*8   metrics          metric_count × (uint16 metric_id, uint16 flags, int32 value)
...     32    hmac             HMAC-SHA256 over bytes 0..(28+n*8), key pinned both sides
```

Frame size is fixed at `28 + MAX_METRICS*8 + 32` bytes; unused metric slots are zero-filled and `metric_count` says how many are live. `MAX_METRICS = 64` for v0.1, giving a 572-byte frame.

**Value encoding.** `value` is `int32`. Each metric ID in the registry declares a fixed scale and unit (e.g. `loss: value/10000`, `episodes: count`, `bundle_status: enum`). There are no floats on the wire. Precision is decided per metric in the registry, not by the sender.

**Flags.** Bit 0: value is a delta since the last frame rather than an absolute. Bit 1: value is stale (sender could not read the source this tick and is repeating). Bits 2–15 reserved, must be zero.

**HMAC.** Not for confidentiality; the channel is dev-only. It lets the reader reject frames from anything inside that is not the designated sender, and gives the parser a cheap first check before it touches any field.

## 5. Sender

The sender is a single inside process, `egress-sender`, with:

- Read access to a metrics socket that inside components write to (run loop, worker, go-live service, receiver).
- Write access to the diode.
- Nothing else. No filesystem access beyond its own config, no access to run artifacts, no LLM.

It ticks on a fixed interval (default 60 s), reads the latest value per registered metric ID from the socket's ring buffer, builds one frame, HMACs it, sends it. If more metrics are pending than `MAX_METRICS`, it sends the lowest-numbered IDs and increments `egress.dropped_metrics` (itself a metric). It never sends more than one frame per tick.

Inside components emit `(metric_id, value)` pairs to the socket. The socket accepts only that pair shape; a component that tries to write anything else gets its write dropped and `egress.bad_writes` incremented. There is no way for a component to add a metric ID that is not in the registry.

## 6. Reader

The reader is `egress-reader`, a few hundred lines with no dependencies beyond an HMAC library:

1. Read exactly one frame's worth of bytes.
2. Check magic. Mismatch → drop, count.
3. Verify HMAC. Mismatch → drop, count.
4. Check `schema_version` and `registry_version` against pinned values. Mismatch → drop, count. (The registry is updated by shipping a new reader binary, not by anything on the wire.)
5. Check `frame_sequence > last_seen`. Otherwise drop, count.
6. Check `metric_count <= MAX_METRICS` and every `metric_id` is in the registry and every reserved flag bit is zero. Any failure → drop the whole frame, count.
7. For each metric: apply the registry's scale, write `(timestamp, run_id, metric_name, scaled_value, flags)` to the local time-series store.

Dropped-frame counters are themselves exposed on the dashboard so a misbehaving sender is visible as a gap plus a rising drop count.

The reader must be written without a general-purpose deserialization library and without any code path that treats frame bytes as anything but integers. Fuzz it against random bytes, truncated frames, and frames with every field at min/max before deployment, and keep the fuzz harness in the repo.

## 7. Bandwidth and the residual channel

At 64 metrics × 32 bits × 1 frame/minute, the channel's ceiling is about 34 bits per second. Practically, most metrics are constrained by their declared scale and semantics, so the usable covert capacity is lower, but assume the ceiling.

What this bounds: an inside process could, over a day, push roughly 370 KB to the dev reader. That is nowhere near model weights and nowhere near a corpus. It is enough for a small prompt or a config file. Since the far side is a dev-owned reader with no internet path, this is a bounded signal to the operators, not an exfiltration. It is listed here so nobody later mistakes the channel for zero-bandwidth.

Tightening knobs if ever needed, in order of cost: lower `MAX_METRICS`; lengthen the tick; narrow per-metric scales in the registry. None of these need a wire-format change.

## 8. Metric registry v1

The registry is a versioned table checked into the repo and compiled into both sender and reader. Names never travel on the wire; only IDs do.

| id | name | scale / unit | flags | source |
|----|------|--------------|-------|--------|
| 1 | `sys.heartbeat` | count, delta | delta | sender |
| 2 | `sys.uptime_s` | seconds | | sender |
| 3 | `egress.dropped_metrics` | count | delta | sender |
| 4 | `egress.bad_writes` | count | delta | sender |
| 10 | `recv.bundles_ok` | count | delta | receiver |
| 11 | `recv.bundles_rejected` | count | delta | receiver |
| 12 | `recv.last_reject_code` | enum (bundle spec §8.3) | | receiver |
| 13 | `recv.sequence_high_water` | count | | receiver |
| 20 | `worker.inbox_depth` | count | | worker |
| 21 | `worker.composed` | count | delta | worker |
| 22 | `worker.failed` | count | delta | worker |
| 23 | `worker.last_status_code` | enum | | worker |
| 24 | `worker.last_subcode` | enum | | worker |
| 30 | `golive.pass` | count | delta | go-live |
| 31 | `golive.fail` | count | delta | go-live |
| 32 | `golive.sandbox_count` | count | | go-live |
| 33 | `golive.last_duration_s` | seconds | | go-live |
| 40 | `sites.live` | count | | registry |
| 41 | `sites.retired` | count | delta | registry |
| 42 | `search.indexed_sites` | count | | search engine |
| 43 | `search.qps` | ×100 | | search engine |
| 50 | `run.state` | enum: 0 idle, 1 running, 2 paused, 3 done, 4 error | | run loop |
| 51 | `run.step` | count | | run loop |
| 52 | `run.episodes` | count, delta | delta | run loop |
| 53 | `run.loss` | ×10000 | | run loop |
| 54 | `run.reward_mean` | ×10000 | | run loop |
| 55 | `run.reward_passk` | ×10000 | | run loop |
| 56 | `run.success_rate` | ×10000 | | run loop |
| 57 | `run.minefield_hits` | count, delta | delta | run loop |
| 58 | `run.collateral_flags` | count, delta | delta | run loop |
| 59 | `run.tokens_per_s` | count | | run loop |
| 60 | `run.lr` | ×1e8 | | run loop |
| 61 | `run.grad_norm` | ×10000 | | run loop |
| 70 | `hw.gpu_util_pct` | ×100 | | node agent |
| 71 | `hw.gpu_mem_pct` | ×100 | | node agent |
| 72 | `hw.disk_free_pct` | ×100 | | node agent |
| 73 | `hw.temp_c` | ×10 | | node agent |
| 74 | `hw.node_count_ok` | count | | node agent |

`run_id` in the frame header distinguishes concurrent runs; metrics 50–61 are per-run, everything else is system-wide and sent with `run_id = 0`.

Adding a metric is a registry version bump, a sender release, and a reader release. Removing one is the same. IDs are never reused.

## 9. What does not go through this channel

Explicitly, so the list is in the doc:

- Log lines, stack traces, exception messages
- Test output, diffs, screenshots
- Prompts, completions, trajectories, transcripts
- File paths, hostnames, bundle IDs as strings (bundle status is an enum; which bundle is at the terminal)
- Checkpoints, weights, eval result tables
- Anything from the decrypted content of any site

All of the above are read at the wired terminal or leave via the reviewed physical export procedure.

## 10. Open questions

1. **Per-site metrics.** Developers will want "which site is failing go-live." Sending `site_id` as an integer is fine technically (it's just a number) but starts turning the channel into an event log. Proposal: a single `golive.last_failed_site` metric, absolute, integer site ID; no history. Revisit after use.
2. **Tick interval during eval.** 60 s is right for training. Short eval runs might finish between ticks. Options: a faster tick with fewer metrics, or accept that short runs are read at the terminal.
3. **Second sender for the terminal room.** If humans at the terminal want to push a "run X reviewed, OK to export" flag to the dashboard, that is a second sender with its own HMAC key and a tiny registry. Worth having; keeps the export procedure visible on the dashboard.
