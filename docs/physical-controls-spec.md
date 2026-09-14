# Airgapped Web Environment — Physical Controls and Hardware Enforcement

**Version:** 0.1 (draft)
**Date:** 2026-09-14
**Companion to:** Bundle Format Specification v0.1, Egress Metrics Channel v0.1, Agent Sandbox v0.1

---

## 1. Purpose

The other specifications in this repository describe software that can be reviewed,
tested, and fuzzed. This one describes the parts of the design that software cannot
enforce, and the parts where **enforcing a rule in hardware is the difference between
a guarantee and a promise**.

The README states three of these in a bullet list — no removable media inside, the
terminal is display-and-keyboard only, two-person export — and notes that the specs
state them but cannot enforce them. That bullet list is not enough to build from.
This document is.

There is one idea running through it. A control implemented by the thing it
constrains is not a control. The egress sender is inside the airgap; if the only
thing limiting it to sixty-four integers a minute is its own source code, then
anything that compromises the inside also compromises the limit. The limit has to
live somewhere the inside cannot reach.

---

## 2. The wired developer station

### 2.1 Why it exists

The egress channel is deliberately too narrow to develop against. Somebody has to
read stack traces, look at failed go-live output, triage a quarantined checkpoint,
and inspect a frozen agent VM after a sandbox halt. All of that happens at a
physically wired station inside the controlled space, on a screen that does not leave
the room.

This is the standard SCIF trade and it should be named as such rather than left
implicit. The diodes do not make the system sealed. They move the highest-bandwidth
egress path from "a misconfigured log shipper" to "a human being with eyes and
pockets," which is a much better place for it, because humans are a threat model
organisations already know how to reason about.

### 2.2 Station requirements

- **Display and keyboard only.** No local mass storage. Boot from read-only media or
  network-boot from inside. If the device has an SSD, it is not this device.
- **No removable media.** USB mass storage disabled in firmware with a set
  supervisor password, and the ports physically blocked. Treat a working USB port on
  this machine as a finding, not an inconvenience.
- **No wireless of any kind.** Wi-Fi and Bluetooth radios removed or disabled in
  firmware, not merely switched off in the OS. Verify on a schedule.
- **Video out only to the room's display.** No capture card, no streaming, no remote
  desktop server, ever. The station does not accept inbound connections.
- **No route off the inside network.** The station is an inside host. It cannot reach
  the reader host, the dev LAN, or the internet, and it is not the host that runs the
  escape watchdog.
- **Session logging.** Who was at the station, when, against which run. This is the
  audit record that makes the two-person export procedure meaningful.

### 2.3 What may be done at the station

Everything the numeric channel deliberately cannot carry: logs, stack traces, test
output, render diffs, screenshots, trajectories, prompts and completions, decrypted
go-live failures, frozen VM memory from a sandbox halt.

Two specific jobs belong here and nowhere else:

1. **Failed go-live triage.** The bundle spec is explicit that a failed go-live never
   returns decrypted logs to the worker, only enum codes. The stack trace exists; a
   human reads it here. This is the requirement that will be under the most pressure
   in practice — somebody will eventually want to "just let the worker see the
   traceback to debug it." That change would hand a prompt-injection surface straight
   back to an LLM inside the trusted zone, and the station is what makes refusing it
   reasonable rather than obstructive.
2. **Sandbox halt forensics.** Per the sandbox spec, only a human at this station
   returns the watchdog from HALTED to CLEAN. There is no remote resume.

### 2.4 Bulk export

Checkpoints, eval transcripts, and datasets do not leave over any diode. They leave
by a reviewed physical transfer:

- Two people, both logged, neither able to complete the procedure alone.
- A written manifest of what is leaving, produced before the transfer, not after.
- Export media that is single-use and accounted for.
- The procedure is refused outright for any checkpoint the escape watchdog
  quarantined (sandbox spec §6.3) until that quarantine is explicitly cleared by a
  named reviewer.

The last point is the one with teeth. A checkpoint produced during a window when the
agent may have reached its own weights is exactly the artifact somebody will be in a
hurry to export.

---

## 3. Hardware enforcement of the egress channel

### 3.1 The problem with a software-only filter

The egress spec describes a sender that ticks once a minute and emits one fixed-size
frame of integers from a pinned registry. Everything in that sentence is enforced by
the sender's own code, and the sender runs inside the airgap. A compromised inside
does not have to defeat the rate cap; it owns the process implementing it. The
software cap is a correctness measure — it stops honest components from flooding the
channel — not a security boundary.

The reader on the far side is a real boundary, because it is outside and it drops
anything that does not match. But the reader can only refuse to *interpret* what
arrives. It cannot stop bytes from crossing the link, and it cannot bound how many
bytes cross per second. The residual channel calculation in the egress spec (§7,
~34 bits/s) silently assumes the sender obeys its own tick.

### 3.2 The established pattern

This is a solved problem in the cross-domain community, and the solution is to put
the filter in **fixed-function hardware on the send side, upstream of the one-way
link**. Transfer cross-domain solutions have moved filtering from software into
FPGA-based data guards that inspect at wire speed, and the NSA's Raise the Bar
baseline formally recognises hardware filtering as part of robust CDS design.
Commercial unidirectional gateways combine hardware-enforced one-way transfer with
FPGA protocol filtering so that only well-formed, policy-approved data crosses, with
non-compliant packets rejected by the source-side FPGA *before* reaching the isolator.

Our case is unusually favourable for this. The published designs have to filter
complicated formats. We have to filter a 572-byte fixed-layout frame of integers.
That fits in a comparator tree.

### 3.3 The egress filter appliance

Sit an FPGA or CPLD between the sender's NIC and the diode's transmit optic:

```
  inside sender ──▶ egress filter ──▶ data diode ──▶ reader host ──▶ dashboard
   (software,        (FPGA, fixed     (one-way,      (outside)
    inside,           function,        hardware)
    untrusted)        bitstream
                      loaded from
                      outside)
```

What it enforces in gates rather than in code:

| Check | Rule |
|-------|------|
| Frame length | exactly 572 bytes; anything else is not forwarded |
| Magic | bytes 0–3 equal the constant |
| Versions | `schema_version` and `registry_version` equal pinned constants |
| Metric count | `metric_count <= 64` |
| Metric IDs | every ID present in a registry bitmap held in the fabric |
| Reserved bits | flags bits 2–15 are zero |
| Sequence | `frame_sequence` strictly increasing |
| **Rate** | **one frame per tick, enforced by a hardware token bucket** |

Properties that matter:

- **No configuration path from the inside.** The bitstream is loaded from outside the
  airgap, and the device exposes no runtime configuration interface to the sender. A
  registry change is a bitstream reflash performed by a human, on the same release
  cadence as the sender and reader releases the egress spec already requires.
- **No parser in the usual sense.** No OS, no allocator, no deserialiser, no dynamic
  dispatch. The malformed-input attack surface that §6 of the egress spec asks you to
  fuzz mostly ceases to exist, though the reader's fuzz harness stays regardless.
- **Fail closed and silent.** A rejected frame is dropped. Nothing is signalled back
  to the sender; a rejection counter is readable only from the outside face. The
  inside cannot use rejection as a side channel because it cannot observe it.

### 3.4 Re-clocking, and why it matters more than it looks

A filter that forwards each frame as it arrives preserves the *timing* of arrivals,
and timing is a channel. A compromised sender that cannot change frame contents can
still modulate when frames appear.

So the filter should not forward. It should **re-clock**: emit exactly one frame per
fixed tick, from its own oscillator, regardless of what arrived. If a valid frame is
waiting, send it. If none arrived, send the previous frame with the stale flag set.
If several arrived, send the newest and drop the rest.

The channel then carries a constant frame rate whose timing is a property of the
filter's clock rather than of anything inside. That closes the timing sub-channel
entirely and makes the egress spec's bandwidth ceiling a real ceiling rather than a
nominal one. It also gives the dashboard a free liveness signal: frames arrive
forever, and the stale flag says whether anyone inside is still talking.

### 3.5 Assurance ladder

Worth stating plainly so nobody over-reads the design:

| Control | Enforced by | Defeated by |
|---------|-------------|-------------|
| Frames are numbers from a registry | FPGA filter | filter bitstream compromise |
| One frame per tick | FPGA token bucket + re-clock | filter bitstream compromise |
| Bytes flow one way only | diode hardware | diode vendor compromise |
| Frames are from the designated sender | HMAC, checked by reader | key compromise inside |
| Reader cannot be exploited by frame bytes | hand-written parser + fuzzing | implementation bug |
| Nothing leaves on removable media | procedure and physical controls | a person |
| Checkpoints are reviewed before export | two-person procedure | two people |

The bottom two rows are not weaker by accident. They are the rows where the design
deliberately stops trying to be clever and relies on an organisation instead.

---

## 4. Ingress hardware

The same argument applies in the other direction with one difference: ingress
already has a cryptographic control that does not depend on trusting the outside
pipeline's rate or shape. Bundles are signed, sequence-numbered, and hash-verified,
and the receiver is inside and rejects anything that fails.

A hardware filter on the ingress side is therefore optional rather than structural.
If deployed, it should enforce only coarse, obviously-correct properties — maximum
bundle size, maximum bundles per interval — as a denial-of-service bound on the
receiver, not as a content control. Content control on ingress is the signature, and
adding a hardware filter that tries to understand bundle contents would add a parser
to the trusted path, which is a step backwards.

---

## 5. Open questions

1. **Filter re-clock versus tick alignment.** If the filter's oscillator and the
   sender's tick drift, the filter periodically sends a stale frame or drops a fresh
   one. Harmless, but the dashboard should not show it as an incident. Needs a
   documented tolerance.
2. **Registry updates as a physical procedure.** Adding a metric already requires a
   sender and a reader release. With the filter it also requires a bitstream reflash
   by someone in the room. That is friction that will tempt people to over-provision
   the registry up front with spare IDs, which is a different kind of mistake. Worth
   deciding deliberately.
3. **Build versus buy.** Commercial diodes from established vendors ship with
   filtering already, evaluated against baselines we would not replicate. Building
   the filter ourselves gives a reviewable bitstream for a format we control. This is
   a real trade and the answer probably depends on who is deploying.
4. **Is the station allowed a printer?** It sounds trivial and it is not: a printer
   is removable media with extra steps, and somebody will want hard copy of an eval
   table. Current leaning: no.
5. **Attestation of the filter.** Nothing currently proves to the outside that the
   filter in the path is the one that was flashed. A verify-on-boot scheme reporting
   to the reader's outside face would help, and is a design in its own right.
