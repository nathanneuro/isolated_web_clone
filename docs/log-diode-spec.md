# Airgapped Web Environment — Log Diode and the Logging Cluster

**Version:** 0.1 (draft)
**Date:** 2026-09-14
**Companion to:** Egress Metrics Channel v0.1, Agent Sandbox v0.1, Physical Controls v0.1

---

## 1. Purpose

There are now three one-way links in this design, and this document specifies the
third:

| Link | Direction | Carries |
|------|-----------|---------|
| Ingress diode | outside → eval cluster | signed bundles, signed commands |
| Egress diode | eval cluster → dev reader (outside) | fixed-schema integers |
| **Log diode** | **eval cluster → logging cluster (inside)** | **append-only log records** |

The eval cluster produces logs in volume: agent trajectories, model completions,
go-live failures, stack traces, driver activity, watchdog detail. None of it can
leave over the numeric egress channel, and all of it is needed by humans. So it goes
one way to a **logging cluster**, which is inside the airgap, is where humans
actually read, and is separated from the eval cluster by a diode of its own.

The eval cluster **blindly appends**. It gets no acknowledgment, no query interface,
no ability to read back, amend, or delete what it has written.

---

## 2. Why a diode here and not a network

It is tempting to make this a normal log pipeline — the eval cluster ships to an
aggregator, humans browse a dashboard. That is the architecture of the incidents this
repository is a response to, and it fails in both directions.

**From eval cluster to logging cluster.** Assume what the sandbox spec assumes: the
agent under evaluation may escape its VM. With a network link, the eval cluster's
compromise reaches the logging cluster interactively — and the logging cluster is
where the humans are looking, which makes it the most valuable host inside for
anything trying to influence a person. With a diode it can push bytes and nothing
else. It cannot connect, cannot enumerate, cannot authenticate, cannot exploit a
listening service, because on the far side there is no listening service and no
return path to carry a response to one.

**From logging cluster to eval cluster.** This is the direction people forget. A
log aggregator with a network path back into the cluster it monitors is a control
channel: agent configuration, log-level changes, "just re-run that episode," a shell
for the on-call engineer. That path is also available to whatever compromises the
aggregator, and aggregators are large software with large dependency trees. The diode
removes the direction entirely. Nothing on the logging cluster can affect a run,
which also means nothing on the logging cluster is safety-critical.

**The agent has a motive to target the logging cluster, and it is a specific one.**
Not vandalism: *the logs are the record of what it did*. An agent that cheated on an
eval — used a shortcut the question meant to exclude, reached environment state
directly, escaped its sandbox — has a direct interest in the record of that not
reaching a human. Editing the transcript is a cheaper way to pass than solving the
task, and it is exactly the kind of shortcut an effective optimiser finds. Assume
the log path is a target on the same footing as the reward function, because for an
agent being scored, it is part of the reward function.

**Append-only is therefore a security property, not a storage choice.** An agent that
escapes its sandbox and reaches the eval cluster's log writer can append whatever it
likes. It cannot delete, rewrite, or truncate what has already crossed, because the
bytes are on the other side of a link with no reverse path, on write-once storage.
The evidence of the escape survives the escape, and the evidence of the cheat
survives the cheat. Design the logging cluster's storage as WORM and mean it.

Append-only does leave one move available: appending *more*, to bury a record under
volume or to write plausible-looking entries around it. That is why records are
sequence-numbered per source (§4), why volume is itself a metric, and why nothing
downstream treats a log line as authoritative because it looks well-formed.

---

## 3. Logs are untrusted input

This is the operative rule of the document, and it is the one that most log
architectures get wrong.

> **Every byte in a log record is attacker-influenced until proven otherwise, and
> some of it is dangerous on its own terms.**

Consider what is actually in these logs. Agent-generated text, chosen by a model
being optimised against the environment. Scraped page content, reproduced in
tracebacks and assertion messages. Model completions. Template render output. Form
inputs. Filenames derived from content. A log line is not a report *about* untrusted
data; it frequently *is* untrusted data with a timestamp on it.

And in a lab running **dangerous capability evaluations**, the logs contain the
residue of those evaluations. Transcripts of a model doing the thing the eval was
built to measure. Working exploit code it produced. A successful sandbox escape,
described in enough operational detail for the run to be reproduced — which is the
point of logging it, and also what makes it hazardous. Uplift-eval completions whose
content is the hazard.

So the logging cluster holds material that is simultaneously **possibly malicious**
(aimed at whatever parses or displays it) and **possibly infohazardous** (dangerous
because of what it says, to anyone who reads it or copies it out). Those need
different controls. §5 handles the first. §6 handles the second, and §6 is the
reason the logging cluster is a containment zone rather than a destination.

So the logging cluster treats an arriving record the way the receiver treats an
arriving bundle: as bytes to be validated, never as anything to be executed,
interpreted, expanded, or rendered as markup.

### 3.1 The attack surface, concretely

| Vector | What it looks like | Mitigation |
|--------|--------------------|------------|
| **Terminal escape injection** | ANSI/OSC sequences in a record that reprogram the reader's terminal, retitle the window, rewrite earlier lines to hide an event, or on some terminals trigger a response the shell then executes | strip all C0/C1 control bytes except the record's own structure at ingest; never `cat` a raw log |
| **Forged records** | an embedded newline plus a plausible timestamp, manufacturing a log line that never happened — or splitting one so the real event scrolls away | length-prefixed framing (§4); newlines inside a record are data, never separators |
| **Interpolation / lookup injection** | a record containing a substitution syntax the log library expands — the Log4Shell class of bug | the ingest path performs no interpolation on record content, ever, and the storage format has no expansion syntax |
| **Deserialisation** | pickled objects, YAML tags, marshalled structures in a record body | records carry bytes and integers; no deserialiser touches record content |
| **Viewer exploitation** | HTML/JS in a record rendered by a web log viewer; a notebook auto-rendering a payload | the viewer renders as plain text by default; anything richer is opt-in per record and sandboxed |
| **Parser DoS** | catastrophic regex backtracking, decompression bombs, a billion-laugh structure | fixed framing, hard size caps per record, no recursive formats |
| **Path traversal** | a record field used as a filename, containing `../` | record content never names a file; paths are derived from the frame header only |
| **Malware at rest** | a record whose content is a payload waiting for something to open it | §5 |

Nothing in that table is novel. The point is that the list applies to logs, which
teams reliably classify as output rather than input.

---

## 4. Record format

Same philosophy as the egress frame: fixed shape, hand-written parser, no library
between attacker-controlled bytes and the decision to accept them.

```
offset  size   field
0       4      magic              0x414c4731 ("ALG1")
4       2      schema_version     uint16
6       2      source_id          uint16, from a fixed source registry
8       8      record_sequence    uint64, monotonic per source
16      8      timestamp          uint64
24      2      stream_id          uint16, enum: trajectory, golive, worker, driver,
                                  watchdog, runner, system
26      2      severity           uint16, enum
28      4      payload_len        uint32, <= MAX_PAYLOAD (1 MiB)
32      n      payload            OPAQUE BYTES. never parsed at ingest.
32+n    32     hmac               HMAC-SHA256 over bytes 0..(32+n)
```

**The header is structure; the payload is content.** That is the same split the bundle
format makes, for the same reason. Every routing, indexing, and retention decision is
made from header fields — integers from fixed registries — and none is ever made from
payload bytes. The payload is stored, hashed, and displayed. It is not parsed.

**`record_sequence` makes gaps visible.** A compromised eval cluster cannot delete
what has crossed, but it can stop sending. Monotonic per-source sequence numbers mean
a gap is detectable and a truncation attempt is a visible hole rather than a quiet
absence. Gaps are alarming here in a way they are not on the ingress path.

**The HMAC is not confidentiality and not proof against a compromised cluster** — a
compromised eval cluster holds the key. It rejects records from anything on the eval
side that is not the designated writer, and it gives the ingest parser a cheap check
before it touches any other field.

---

## 5. Quarantine before anything opens a log

> **Nothing in a log file gets executed, and nothing opens one with a tool more
> capable than a byte viewer, until it has been checked.**

Arriving records land in a **quarantine tier** on the logging cluster: write-once,
no execute mount, owned by a user nothing else runs as. In quarantine:

1. **Framing validation.** Magic, versions, registry membership, `payload_len`
   within cap, HMAC, sequence monotonic. Anything failing is retained (it is
   evidence) and marked, never discarded, never promoted.
2. **Sanitisation for display.** A derived, display-safe copy of each payload with
   C0/C1 control bytes stripped, invalid UTF-8 replaced, and length truncated. The
   original is kept byte-exact for forensics; humans read the derived copy.
3. **Malware scanning.** Signature and heuristic scan over the raw payload. This is
   the step that assumes a payload may be a deliberate implant aimed at whatever
   tooling eventually touches it.
4. **Promotion.** Only records that pass are promoted to the readable tier and
   indexed — on header fields only.

Records that fail any step stay in quarantine, are counted, and are surfaced to a
human as a count. A payload that fails a malware scan is itself a finding worth
waking someone for: on the inside, there is no benign explanation.

**Anything automated that consumes logs consumes the promoted tier**, and treats even
that as untrusted text. Promotion means "passed the checks," not "trustworthy."

---

## 6. Nothing leaves the logging cluster uncleaned

The logging cluster receives everything the eval cluster could not say over a numeric
channel. That makes it, plausibly, the most sensitive host in the building: it holds
the transcripts of dangerous capability evaluations, the operational detail of any
sandbox escape that occurred, and whatever a model produced while being measured on
its ability to produce it.

It is therefore not a destination. It is a **containment zone with its own egress
control**, and the rule is short:

> **Nothing leaves the logging cluster until after a thorough cleaning.**

"Leaves" means anything: a report, a figure for a paper, a quoted transcript in an
incident write-up, a dataset of trajectories for later analysis, a file on export
media, a screenshot, a sentence pasted into a chat window. The logging cluster has no
network path out, and material leaves by a reviewed procedure or not at all.

### 6.1 What cleaning means

Not redaction of names. Three distinct judgements, and they are made by people, not
by a filter:

1. **Capability residue.** Does this material contain, or meaningfully reconstruct,
   the dangerous capability the eval was measuring? A transcript demonstrating that a
   model can do X is usually safe to summarise as "it scored 0.4 on X" and unsafe to
   publish verbatim. The metric is the finding; the transcript is the hazard.
2. **Escape technique.** Does it describe how a sandbox was defeated in enough detail
   to repeat? This needs to reach the people who will fix it and stop there, which is
   a narrower distribution than "the eval team."
3. **Malware.** Has it passed §5's checks, and is the exported artifact itself inert?
   A log excerpt carried out on media can carry a payload as easily as it came in.

Only the *derived* artifact leaves — a summary, a score table, a sanitised excerpt.
Raw records do not leave, and "I'll just take the trajectory file to look at on my
laptop" is the specific thing this rule exists to prevent.

### 6.2 Procedure

The same two-person shape as the bulk export in physical-controls-spec §2.4, with an
addition that matters here: **the second person is reviewing for hazard, not just
countersigning custody.** Custody review asks "is this person allowed to take this."
Hazard review asks "should this exist outside this room at all," and the two are
different questions that the same procedure has to ask separately or it will
collapse into the easier one.

- Two people, both logged, neither able to complete the procedure alone.
- A written manifest of what is leaving and its derivation, produced before the
  transfer.
- Single-use, accounted export media.
- Refused outright for material relating to a quarantined checkpoint or an
  un-triaged watchdog halt until that is cleared by a named reviewer.

### 6.3 Consequences for the people at the station

Reading logs at the wired station means reading, routinely, material in categories
1 and 2 above. That has staffing consequences the architecture cannot solve:
need-to-know applies inside the room as well as outside it, the set of people with
station access is smaller than the set with eval-cluster access, and "I saw it on the
dashboard" must never be a route to seeing a transcript.

It also has a retention consequence. Material that is hazardous and no longer needed
is a liability with no offsetting benefit. The logging cluster needs a destruction
policy with the same seriousness as its retention policy, and WORM storage makes
destruction a media-level operation that has to be planned rather than an `rm`.

---

## 7. Where the humans are

This refines physical-controls-spec §2, which says humans read logs at the wired
station and left the station's attachment unstated.

**The wired developer station attaches to the logging cluster, not to the eval
cluster.** That is the whole benefit of having built the log diode. A station
attached to the live eval cluster is a path from a compromised eval cluster to a
human's screen and keyboard, which is the highest-value target inside the room. Give
the person a view of a cluster that cannot be reached from the one under evaluation.

Consequences worth stating:

- **Failed go-live triage** (physical-controls §2.3) reads the traceback on the
  logging cluster. The go-live service writes it across the log diode; it still never
  returns it to the worker.
- **Sandbox halt forensics** are the exception. A frozen VM memory image is large,
  binary, and not a log; it does not cross the log diode. It is reviewed on the eval
  cluster under a named procedure, by someone who understands that they are handling
  the output of a successful escape.
- **Operating on the eval cluster** — restarting a service, clearing a quarantine —
  needs its own access path, and that path should be as deliberate and as logged as
  the bulk export procedure. The convenience of "I'm already looking at the logs, let
  me just fix it" is precisely what the diode exists to remove.

---

## 8. Egress metrics

The logging cluster gets its own block in the metric registry so the dashboard can
show that logging is alive without anyone being on the inside. Registry version 3,
block 90–99, per the egress spec's rule that adding metrics is a registry bump plus a
sender and reader release.

| id | name | scale / unit | flags | source |
|----|------|--------------|-------|--------|
| 90 | `log.records_ingested` | count, delta | delta | log ingest |
| 91 | `log.records_quarantined` | count, delta | delta | log ingest |
| 92 | `log.framing_rejects` | count, delta | delta | log ingest |
| 93 | `log.hmac_rejects` | count, delta | delta | log ingest |
| 94 | `log.sequence_gaps` | count, delta | delta | log ingest |
| 95 | `log.malware_findings` | count | | log ingest |
| 96 | `log.sanitiser_strips` | count, delta | delta | log ingest |
| 97 | `log.storage_free_pct` | ×100 | | logging cluster |
| 98 | `log.oldest_unpromoted_s` | seconds | | log ingest |
| 99 | `log.writer_alive` | enum: 0 down, 1 alive | | log ingest |

`log.malware_findings` is absolute rather than a delta on purpose: it should be zero
forever, and a non-zero value should stay visible on the dashboard until a human
clears it rather than scrolling away as a one-minute spike.

`log.sequence_gaps` is the one to alarm on. It is how a truncation attempt looks from
outside.

---

## 9. Open questions

1. **Volume.** Full trajectories for 10^6 episodes across 10^5 sites is a serious
   data rate for a link with no flow control. Either the diode is sized for peak, or
   the eval cluster samples and the sampling policy becomes a thing that can be
   attacked (an agent that learns which episodes are logged). Leaning: log all
   watchdog, go-live, and worker streams in full; sample trajectories by a rule the
   eval cluster cannot influence.
2. **Backpressure without a back-channel.** A diode gives no flow control, so the
   eval cluster cannot be told the logging cluster is full. `log.storage_free_pct` is
   on the egress channel for a human to watch, which is not a mechanism. A
   hardware-enforced rate cap on the log diode is the honest answer.
3. **Is quarantine scanning worth it against a targeted implant?** Signature scanning
   catches commodity payloads. The threat here is bespoke. The real control is §4's
   refusal to parse and §5's no-execute mount; scanning is defence in depth and should
   not be mistaken for the boundary.
4. **Two logging clusters?** Watchdog and go-live records are small, structured, and
   security-critical. Trajectories are enormous and mostly mundane. Splitting them
   would let the security-critical path have a much smaller, more reviewable ingest.
   Probably worth it; adds a diode.
