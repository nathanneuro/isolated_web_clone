# Airgapped Web Environment — Remote Sites and Red/Black Links

**Version:** 0.1 (draft)
**Date:** 2026-09-25
**Companion to:** Egress Metrics Channel v0.1, Log Diode v0.1, Physical Controls v0.1

---

## 1. Purpose

Developers want to reach the airgapped cluster from somewhere other than the room it
sits in. Sometimes that means a dashboard at home. Sometimes it means an outside
pipeline in one building feeding an eval cluster in another. Sometimes it means a
reviewer at another site who needs to read the logs. This document says which of
those the design allows, how to build them, and which one it refuses.

The mechanism is standard. Militaries and governments link classified enclaves
across untrusted networks with **red/black separation** and **inline network
encryptors**, also called "bump-in-the-wire" encryptors. The plaintext (red) side
and the ciphertext (black) side are separate interfaces. The box treats traffic as
opaque, and it talks only to preconfigured peers.

Two rules govern every link this document allows.

> **A tunnel is a cable.** Everything the other specs say about who may be cabled to
> whom applies unchanged. An encryptor pair adds confidentiality and integrity over
> distance. It adds no permission.

> **Diodes stay at the walls.** Every enclave boundary a link crosses keeps its
> diode. The encryptor goes between the diodes, never in place of one.

The limitation is worth stating before anything else. A tunnel constrains *where*
traffic goes, not *what* it carries. If the host at one end is compromised, the
other end is fully reachable through it. For this design that is decisive: the
eval cluster is assumed compromisable by the agent under evaluation (agent-sandbox
spec §2). A two-way tunnel from it to anywhere gives that agent a route. So the eval
cluster never gets one (§3.4).

---

## 2. Existing implementations, and what each gives us

| Implementation | Shape | Fits this design? |
|---|---|---|
| **HAIPE / Type 1** (e.g. General Dynamics TACLANE) | Red and black on physically separate interfaces, often separate processors. Opaque traffic, preconfigured peers, traffic-flow security available. | Yes, where procurement allows it. It is the assurance target the reference code describes. |
| **NSA CSfC** | Two independent encryption layers from different vendors, e.g. IPsec outside with MACsec or TLS inside. A bug in one does not break the link. | Yes. The layering principle is adopted in §5.2 even where the products are not. |
| **DIY: WireGuard between two small routers** | OpenBSD or Linux box per side, pinned peer keys, firewall drops everything but the tunnel, target's only cable goes to its gateway. | Only for two-way links between outside-class hosts (§3.2). WireGuard needs a handshake, so it cannot run through a diode. |

The last row is the reason this repo carries its own reference protocol. Every link
that reaches the inside in this design is one-way. A protocol that needs a reply
before it sends data cannot run over one-way hardware. You would have to add a
return path to use it, which is the path the design exists to remove. §4 specifies a
handshake-less protocol that does run over a diode, and `tools/black_link/` holds the
reference implementation.

---

## 3. What may be linked

### 3.1 One-way links carried between sites (allowed)

Each of the three existing one-way links (log-diode-spec §1) can cross a WAN. The
shape is the same for all three:

```
 source ─▶ [wall diode] ─▶ enc A ─▶ [tx diode] ─▶ WAN ─▶ dec B ─▶ [rx diode] ─▶ destination
           (existing)      (red→black)  (optional)          (black→red) (if dest is
                                                                        inside or reader)
```

- **The wall diode** is the one that exists today. Nothing about it changes.
- **Encryptor A** is the sending end of `black-link`: one cell per tick, cover when
  idle. Its black interface only transmits.
- **The tx diode** makes that property physical. A's black side receives nothing
  from the WAN, so nothing on the WAN can attack A. This is possible only because
  the protocol has no handshake. Recommended wherever the link is not trivially
  low-value.
- **Decryptor B** is the receiving end. Its black side faces the WAN and takes
  anything anyone sends, so it is the one component here with an internet-facing
  attack surface. That surface is small: a size check, a magic, a link id, a replay
  window, then one AEAD verify. Nothing more complex runs until the tag verifies
  (§4.3).
- **The rx diode** keeps a compromised B from reaching the destination
  interactively. B can still push bytes into the destination, but every destination
  here is already a fail-closed parser of untrusted bytes: the bundle receiver, the
  egress reader, or log ingest. The rx diode is required when the destination is
  inside or is the egress reader host (egress spec §3: "no inbound network path
  except the diode").

Concretely:

| Link | Remote end | Wall diode | tx diode | rx diode |
|---|---|---|---|---|
| **Ingress** | outside pipeline or dev signing station at another site | the existing ingress diode, *after* B | recommended | the ingress diode already is one |
| **Egress** | a reader at the dev's site | the existing egress diode + FPGA filter, *before* A | recommended | **required** |
| **Log** | a logging cluster at another controlled site | the existing log diode, *before* A | **required** | **required** |

The log row is the strict one because both ends are inside. A and B are the only
hosts in that chain with a WAN interface, and each end of the chain has a diode
between it and them.

### 3.2 Two-way links between outside-class hosts (allowed, ordinary engineering)

Outside hosts may be joined like any two corporate networks. Examples are the
reconstruction farm and bundle-build, or bundle-build and the signing station.
WireGuard with pinned keys or a CSfC stack is fine. These links carry nothing the
airgap depends on. Bundles are signed, content is already encrypted to the go-live
key, and the ingress receiver trusts signatures, not the network a bundle came
over.

The one rule that does apply: **a two-way link never terminates on the reader
host.** The egress spec gives the reader host exactly two interfaces, the diode and
the dashboard LAN. Do not give it a third. See §3.3.

### 3.3 What a remote developer gets

Without any exception to the invariants, a developer at another site has:

1. **The dashboard.** Egress frames are carried one-way (§3.1, egress row) to a
   **reader at the developer's site**. Readers are small, cheap, and identical. Run
   one per site instead of extending one reader's LAN across a WAN. Ship frames, not
   dashboards. Each reader keeps its own replay state and its own drop counters.
2. **Commands.** A signing station at the developer's site produces dev-signed
   command and eval bundles. These cross by the ingress row of §3.1. The signing key
   stays with the developer (bundle-format-spec §9), and the receiver's sequence
   check still kills replay whatever path the bundle took.
3. **Ingress of their own site work.** The same, for pipeline-signed bundles.

That is the whole remote surface that needs no exception. It is also everything the
design already gives a developer on the dev LAN. Remote access here means that
surface at a distance, not a new surface.

### 3.4 Refused

- **An encryptor on the eval cluster's red network.** An escaped agent that reaches
  the red side of an encryptor has one hop to a box with an internet-facing
  interface. If it compromises that box, it has the internet. The eval cluster's
  only links stay the three diodes. Splitting the eval cluster's zones (agent,
  inference, environment) across sites with a tunnel is refused for the same reason,
  even though broker-to-inference traffic is two-way on a cable today.
- **A tunnel that ends on a general-purpose machine.** No laptop, desktop, or
  jump host is a peer. A peer is a dedicated encryptor with nothing else on it.
- **Interactive access to anything inside from outside.** This includes SSH, remote
  desktop, and "just a read-only web view." It is invariant 1, restated.
- **Any tunnel that bridges two hosts the other specs keep apart.** A tunnel is a
  cable (§1). If the cable would be refused, the tunnel is refused.

### 3.5 A remote wired station (exception, not recommended)

A reviewer at another site sometimes has to read logs: failed go-live traces,
trajectories, watchdog detail. The design's answer is that they travel to the room.
The alternative below is documented because someone will ask for it, and a written
exception is better than an improvised one.

A remote station is a **second controlled room**, joined to the **logging cluster
only** by a two-way encrypted link. Its requirements are:

- The remote room meets physical-controls-spec §2.2 in full: display and keyboard,
  no storage, no removable media, no radios, session logging. The room is part of
  the enclave. It is not a remote view into it.
- The link terminates on the logging cluster, never the eval cluster (log-diode-spec
  §7). The log diode keeps the eval cluster unreachable from it.
- Two independent encryption layers from different vendors (CSfC shape), Type 1
  where procurement allows.
- Traffic-flow security on both directions (§4.5). Without it, which records a
  reviewer opens, and when, is visible to anyone on the wire.
- Red and black on separate hardware (§5.1).
- Log-diode-spec §6 still governs: a screen in the remote room is inside the
  containment zone, and nothing leaves it uncleaned.

What it costs, stated plainly: the logging cluster, which log-diode-spec §6 calls
plausibly the most sensitive host in the building, gets a two-way path to a box with
an internet-facing interface. It is an **exception to invariant 1**. Adopting it
means amending the README's invariant to name this path, not reading around it. The
reference code does not implement it.

---

## 4. The one-way link protocol (`black-link`)

### 4.1 Why no handshake

Two-way protocols authenticate peers with a handshake and then derive session keys.
Over a diode there is no second leg. So `black-link` uses a **32-byte symmetric key
per link per direction**, generated once and carried to both ends on accounted media.
This is the same custody model as the go-live wrapping key and the egress HMAC key.
The nonce is the link id plus a monotonic counter. Because a key is only ever used
in one direction, no two cells share a nonce.

Symmetric pre-shared keys have a second property worth having. A recorded black-side
capture cannot later be decrypted by breaking a public-key exchange, because there
isn't one.

### 4.2 Cell format

Every cell is **1280 bytes**, the IPv6 minimum MTU. That is one cell per datagram on
any path, and no IP fragmentation on the black network. Without fragmentation, no
reassembly code runs in front of the tag check.

```
offset  size   field
0       4      magic       0x424C4B31 ("BLK1")       authenticated (AAD)
4       4      link_id     uint32                    authenticated (AAD)
8       8      counter     uint64                    authenticated (AAD), nonce
16      1248   ciphertext  XChaCha20                 encrypted, authenticated
1264    16     tag         Poly1305

inner plaintext, read only after the tag verifies:
0       1      kind        0 cover, 1 data
1       1      reserved    zero
2       4      msg_seq     uint32
6       2      frag_idx    uint16
8       2      frag_count  uint16, >= 1
10      2      length      uint16, <= 1236
12      1236   payload     then zero padding
```

Cipher and library follow the repo's crypto rules: XChaCha20-Poly1305 through
`pynacl`, nothing hand-rolled. A cover cell is encrypted exactly like a data cell.
Every fragment but the last is full. That means a message's length follows from its
fragment count and its last fragment, and there is no total-length field for a
receiver to trust.

An egress frame (572 bytes) fits in one cell. A log record up to 1 MiB is at most
849 cells. A bundle is as many cells as it needs.

### 4.3 Receive order

1. Size is exactly 1280. Magic matches. Link id matches.
2. The counter is fresh against a 1024-wide sliding replay window. This is the
   IPsec shape: it tolerates reordering on the WAN and refuses repeats. It is
   checked before the tag so that replay floods cost no crypto, but it is
   **recorded only after** the tag verifies.
3. The Poly1305 tag verifies over header and ciphertext.
4. Only now is any inner field read. Kind is known. Reserved is zero. Fragment
   shape is legal. Padding is zero.
5. The fragment joins a reassembly buffer. The buffer is bounded in open messages
   and in **bytes actually buffered**, never by what a header claims will arrive.
   Messages that stall past their own length plus slack are abandoned.

Steps 1–3 are everything a stranger on the WAN can reach. Steps 4–5 are reachable
only with the key, which means a compromised peer or a bug. They are still
hand-written, fail closed, and fuzzed, because "only reachable with the key" is not
the same as unreachable.

### 4.4 Loss and copies

There is no retransmit, because there is no one to ask. A lost cell loses its
message. Each link handles that the way it already handles a diode drop:

- **Egress.** A lost frame is a gap. The next tick's frame supersedes it, and the
  reader already shows gaps (egress spec §6).
- **Ingress.** A lost bundle is a sequence gap the receiver logs and permits
  (bundle-format-spec §4). The outside notices through `golive` and `sites.live` on
  the dashboard, and resends as a new, newly signed bundle.
- **Log.** A lost record is a `log.sequence_gaps` increment. That metric is the
  one to alarm on, so a lossy WAN on the log link needs copies.

`copies = k` sends every message's fragments k times over. The receiver fills gaps in
one copy from another and delivers the message once. Duplicate copies are counted,
not errors. This is repetition coding, and it costs k times the bandwidth.

### 4.5 Traffic-flow security

The sender emits **exactly one cell per tick from its own clock**, whether or not it
has data. Idle ticks carry cover cells. A watcher on the black network sees a
constant rate of identically sized cells, and cannot tell whether any of them carry
data, how much, or when a burst began.

This matters most on the egress and log links. A compromised inside can't change
what the diodes let through, but it can modulate *when* and *how much* it sends,
and on a WAN that modulation is visible to anyone on the path. The egress filter
already re-clocks frames before the diode (physical-controls-spec §3.4). A
constant-rate encryptor is a second, independent re-clock downstream of the diode,
so the timing on the WAN belongs to A's oscillator and to nothing inside.

The cost is fixed bandwidth at all times. Size each link's tick for its peak:

| Link | Cell rate | Carries |
|---|---|---|
| Egress | 1 per egress tick | one frame per cell, with the rest cover |
| Ingress | chosen per site | bundles, at a rate set by how fast go-live consumes them |
| Log | sized for peak | log-diode-spec §9.1's open question, now also a WAN bill |

A queue that overflows refuses the message locally, on the sending side. The refusal
never crosses the link.

### 4.6 Counters and where they are read

The sender reserves counters on disk in blocks of 65,536. Each reservation is
fsync'd before any counter in it is used, so a crash skips ahead instead of
reusing a nonce. When counter or message-sequence space runs out, the key is
rotated, and rotation is a physical procedure.

Each end's counters (cells accepted and dropped by reason, cover cells, messages
delivered and abandoned) are read **at that end**. They are never sent to the
other end, and never carried on the egress channel. The egress filter's rejection
counter follows the same rule (physical-controls-spec §3.3), and for the same
reason: a rejection the sender can observe is a side channel. None of this runs
inside the airgap, so none of it is in the metric registry.

---

## 5. Hardware and assurance

### 5.1 Separation

The reference code is a Python process, and it gives none of the following. A
deployment needs:

- **Red and black on separate hardware.** Separate NICs at minimum. Separate
  processors or an FPGA crypto path where available. A control-plane compromise on
  one side must not bridge to the other.
- **No management interface on the black side.** Configuration and key load happen
  from the red side, or physically.
- **Diodes as specified in §3.1.** With them, A cannot be reached from the WAN, and
  a compromised B reaches its destination only as a stream of bytes.

### 5.2 Two layers

For the log link, and for any link that carries anything a WAN observer would want,
follow the CSfC shape. Put `black-link` (or its hardware equivalent) inside an
independent second layer from a different implementation, for example a commercial
encryptor running manually keyed, handshake-less SAs. A bug in either layer then
leaves the other standing. Both layers must be one-way-capable, or the diodes of
§3.1 cannot be fitted.

### 5.3 Assurance ladder

| Control | Enforced by | Defeated by |
|---|---|---|
| A WAN stranger's bytes never reach a parser | tag verify before any inner field | AEAD break, implementation bug |
| The sending end cannot be attacked from the WAN | tx diode | diode vendor compromise |
| A compromised receiving end cannot reach its destination interactively | rx diode | diode vendor compromise |
| No cell is replayed | counter window, durable reservation | reservation file loss (rotate the key) |
| Timing and volume on the WAN say nothing | constant-rate sender, cover cells | sender compromise *on the sending side* |
| The eval cluster has no tunnel | this document, §3.4 | a person |
| The remote station room is controlled | physical-controls-spec §2.2 | a person |

### 5.4 Test and fuzz requirements

`tests/test_black_link.py` holds the reference harness. Any replacement
implementation must pass the same properties:

- arbitrary bytes, truncations, and every single-byte flip of a valid cell are
  refused and never raise;
- arbitrary *authenticated* inner plaintext (what a peer holding the key can send)
  never raises and never grows the reassembly buffer past its byte bound;
- every cell is the same size whatever it carries, and cover ciphertext is not
  distinguishable from data ciphertext by content;
- no counter repeats across a sender restart.

---

## 6. Open questions

1. **Forward error correction.** Repetition is the simplest code that works, and
   the most expensive. A fountain code over each message's fragments would cost far
   less on the log link. It would also add a decoder behind the tag check, and that
   decoder is more parser than this design has wanted so far.
2. **Key rotation cadence.** Counter space never runs out in practice. The reason to
   rotate is exposure. Physical rotation on a fixed schedule is simple. A rotation
   message carried over the link itself would be signed by an offline key, as
   `rotate_verification_key` is, but it is a design in its own right.
3. **Attestation of the encryptors.** The same question as the egress filter
   (physical-controls-spec §5, question 5). Nothing proves to either end that the box in the
   path is the one that was provisioned.
4. **Should §3.5 exist at all?** Current leaning: keep it written down and keep it
   unimplemented. Travel is cheaper than the exception.
