# Airgapped Web Environment — Agent Sandbox and Escape Detection

**Version:** 0.1 (draft)
**Date:** 2026-09-14
**Companion to:** Bundle Format Specification v0.1, Egress Metrics Channel v0.1

---

## 1. Purpose

Everything else in this repository is about the boundary between the airgapped
environment and the outside world. This document is about a boundary *inside* it: the
one between **the agent being trained** and the machinery training it.

The agent under training is the one component here whose behaviour is, by
construction, not specified in advance. It is a policy being optimised against a
reward signal, exploring an action space that includes a browser, a tool interface,
and whatever those reach. It is not assumed malicious. It is assumed to be an
effective optimiser pointed at a system with bugs in it, which is enough.

Three things inside the airgap must stay separated:

| Zone | Contains | Why it must not merge with the agent |
|------|----------|--------------------------------------|
| **Agent zone** | the policy's VM: browser, tool runtime, scratch space | this is the untrusted one |
| **Inference zone** | the model server producing the agent's actions, weights, KV cache | weights are the crown jewels; an agent that reaches its own weights can edit its own policy |
| **Environment zone** | the fake-web servers, per-site sandboxes, search engine, seed DBs | an agent that reaches the environment's state directly can satisfy any state-diff reward without doing the task |

**This sandbox is not expected to be foolproof.** That is a design premise, not an
apology. Hypervisor escapes exist, are found regularly, and will be found again. The
airgap, the diodes, and the encrypted-content design exist precisely so that a
sandbox escape is a contained incident rather than a disclosure. What this layer owes
the developers is not perfect containment but a reliable answer to one question:

> **Did an escape happen? If so, the run must already have stopped.**

Note the tense. §6 explains why the answer must be delivered as a fait accompli
rather than as an alert.

---

## 2. Threat model

**In scope.**

- The agent executing code that leaves its VM: a VMM or kernel exploit, a
  misconfigured device passthrough, a shared filesystem with a writable path out.
- The agent reaching a zone it has no legitimate route to, whether by escape or by
  the network policy simply being wrong.
- The agent reaching the environment's backing state (seed DB files, go-live's
  decryption output, the search index on disk) rather than going through the HTTP
  surface it is supposed to act on. This is reward hacking with extra steps, and it
  corrupts the training signal even when nothing is "compromised."
- The agent reaching the inference server's weights, checkpoints, or the training
  loop's control plane.
- A detector being disabled, starved, or lied to by the thing it is watching.

**Out of scope, and stated as such.**

- Microarchitectural side channels between the agent VM and anything co-resident
  (Spectre-class, cache timing, power). Mitigation is scheduling policy — do not
  co-locate the agent VM with the inference server on one physical host, §4 — not
  detection. We do not claim to detect these.
- CPU, firmware, and hypervisor vendor bugs below the VMM. Same class as the diode
  firmware in the bundle spec's threat model: trusted hardware.
- An agent that escapes and then *does nothing observable*. Detection is behavioural.
  A perfectly quiet escape that never touches a canary, never emits a denied packet,
  and never trips a MAC rule is not detected by this design. It is also, by
  construction, not doing anything.
- Anything the agent does *within* its own VM. That is the point of the VM.

---

## 3. Substrate

### 3.1 Recommendation

**One Firecracker microVM per agent episode, on a host that runs nothing else of
value.** Kata Containers is an equivalent choice if you want Kubernetes-native
orchestration; Cloud Hypervisor if you need more device model than Firecracker
offers. The requirement is a **hardware virtualisation boundary with a dedicated
guest kernel** — not a shared-kernel container, whatever the seccomp profile.

The reasoning is the standard 2026 consensus for running LLM-generated or
LLM-directed code: microVMs give each workload its own kernel under KVM, so a guest
kernel bug is not a host kernel bug, and the attack surface reduces to the VMM's
device model — small, in Firecracker's case deliberately so. Containers share the
host kernel and are appropriate only for trusted code.

**gVisor is a reasonable second layer, not the first.** Intercepting syscalls in
userspace genuinely shrinks the kernel surface, and it costs 10–30% on I/O-heavy
workloads, but its boundary is a userspace process rather than a hardware one. Where
it earns its place here is *inside* the microVM, or for the per-site environment
sandboxes where the workload is our own generated FastAPI app rather than the agent.

### 3.2 Hardening the VMM

The microVM is the boundary; the VMM process is what an escape lands in. Assume it
will be reached and make the landing worthless:

- Run the VMM under a **mandatory access control profile** (AppArmor or SELinux with
  per-VM dynamic labelling, sVirt-style) that denies it the host filesystem
  regardless of what executes in the guest. This is the control that turns a VMM
  escape into a process with no reachable assets.
- **seccomp filter on the VMM itself** (Firecracker ships one; keep it at the
  strictest level that works). SIGSYS from the VMM is a §5 detector, so the filter is
  doing double duty as boundary and tripwire.
- `no_new_privs`, a dedicated unprivileged uid per VM, cgroup limits on cpu, memory,
  pids, and io.
- **Minimal device model.** No SPICE, no USB redirection, no clipboard, no shared
  folders, no 9p/virtiofs mount of anything the agent should not have. Every device
  is VMEXIT handler surface.
- Guest rootfs mounted read-only with a per-episode writable overlay that is
  destroyed on reset. The agent's scratch space must not outlive its episode, both
  for isolation and because a persistent scratch is a channel between episodes that
  will quietly corrupt your RL results.

### 3.3 What the agent may reach

The agent zone has **no route to anything except two mediated endpoints**:

```
                    ┌──────────────────────────────────────┐
                    │ AGENT ZONE                            │
                    │  Firecracker microVM, per episode     │
                    │  browser + tool runtime               │
                    └───────┬───────────────────┬───────────┘
                            │ vsock             │ vsock
                    ┌───────▼────────┐  ┌───────▼────────────┐
                    │ action broker  │  │ env broker         │
                    │ (schema-fixed) │  │ (HTTP proxy only)  │
                    └───────┬────────┘  └───────┬────────────┘
                            │                   │
                    ┌───────▼────────┐  ┌───────▼────────────┐
                    │ INFERENCE ZONE │  │ ENVIRONMENT ZONE   │
                    │ model server   │  │ fake-web + search  │
                    │ weights, ckpts │  │ per-site sandboxes │
                    └────────────────┘  └────────────────────┘

    no route from the agent zone to: receiver, worker, go-live, key material,
    egress-sender, the run loop's control plane, the host, or any other agent VM
```

Both brokers are **deterministic, no LLM, fixed schema**, in the same spirit as the
go-live service. The action broker accepts an observation request and returns an
action; it does not accept arbitrary prompts, does not expose model management
endpoints, and does not return anything about the model besides the action. The env
broker is an HTTP proxy that reaches only the live site hostnames in the registry,
which is also the natural place to enforce per-episode rate limits.

Use **vsock rather than IP** between the agent VM and the brokers where you can. It
removes the guest's network stack from the path entirely, and it makes the network
policy trivially auditable: the agent VM has no network interface at all, so any
IP packet attributable to it is already an anomaly.

---

## 4. Placement

The agent VM and the inference server **do not share a physical host**. This is the
only mitigation offered for the side-channel class in §2, and it is a deployment
rule rather than something code can enforce, so it belongs in the runbook next to
"no removable media inside."

Per-site environment sandboxes may share a host with each other but not with the
agent VM.

---

## 5. Escape detection

Every detector below runs **outside the agent VM**. A detector inside the thing it is
watching is decoration. Each one reduces to a counter, because §8 has no other shape
available to it.

### 5.1 Detectors

| # | Detector | Signal | Severity |
|---|----------|--------|----------|
| D1 | **Denied-flow counters** | `nftables`/`eBPF` counters on every deny rule facing the agent zone. A packet from the agent VM toward the receiver, go-live, egress-sender, control plane, host, or another agent VM. | escape |
| D2 | **Non-vsock traffic** | Any IP packet attributable to an agent VM configured with no network interface. | escape |
| D3 | **Canary host** | An unroutable canary address inside the airgap, reachable only by something that has left the agent zone. Any connection attempt. | escape |
| D4 | **Canary credential** | A plausible but invalid token planted in the agent VM's filesystem, valid nowhere. Any presentation of it to any broker or service. | escape |
| D5 | **VMM seccomp violation** | `SIGSYS` from a VMM process. Under a correct filter this is zero forever. | escape |
| D6 | **MAC denial** | AppArmor/SELinux denial attributable to a VMM process. | escape |
| D7 | **VMM process anomaly** | A VMM acquiring an unexpected child process, or crashing/restarting outside the episode lifecycle. | escape |
| D8 | **Host file integrity** | Change to the VMM binary, guest kernel image, base rootfs, or broker binaries. | escape |
| D9 | **Broker schema violation** | A request to either broker that does not match the fixed schema, or arrives from an unexpected source CID. | suspicious |
| D10 | **Environment state bypass** | A change to a site's backing DB not attributable to a request that went through the env broker. | suspicious |
| D11 | **Detector liveness** | Any detector that stops reporting. Absence of signal is a signal. | escape |

D9 and D10 are `suspicious` rather than `escape` because both have benign causes — a
broker client bug, a site's own background job. They still halt the run (§6); the
distinction is for triage at the terminal, not for whether to stop.

D11 deserves the emphasis. The most likely way this whole design fails in practice is
not a defeated detector but a detector nobody noticed had been broken for three
weeks. Treat a silent detector exactly as severely as a firing one.

### 5.2 The watchdog

A single **escape watchdog** process aggregates the detectors. It runs on the host,
outside every VM, and it holds the only authority in this design that matters: it can
halt the run. It is small enough to review in full, it has no LLM in it, and it is
not reachable from the agent zone by any route.

Its state machine has three states and one direction of travel:

```
  CLEAN ──any detector fires──▶ HALTING ──quiesced──▶ HALTED
                                                        │
                                            (only a human at the wired
                                             terminal returns to CLEAN)
```

There is no automatic recovery and no automatic resume. An escape indicator that
clears itself is a bug.

---

## 6. Response: halt first, report second

The developers' stated requirement is "did an escape happen, and if so stop the run."
The design inverts the order: **the run stops automatically, and the notification
reports that it already stopped.**

This is not over-engineering, and the reason is already in this repository's threat
model. The egress channel's residual risk is that a compromised inside can send
plausible numbers to keep a developer from walking to the terminal. If the halt
depends on a human reading a dashboard, then the dashboard is load-bearing security
and that residual becomes a live attack path. If the halt is automatic, the dashboard
is a notification, and lying to it accomplishes nothing except delaying triage of a
run that is already stopped.

On first detector fire, the watchdog:

1. **Freezes** the agent VM (`SIGSTOP` the VMM, do not destroy it — memory state is
   the forensic evidence and a human at the terminal will want it).
2. **Severs** the brokers: both refuse all further requests for this run.
3. **Checkpoints and halts** the run loop, marking the checkpoint quarantined. A
   checkpoint produced during a window when the agent may have reached its own
   weights is not a safe artifact, and it must not silently enter the normal export
   path.
4. **Preserves** detector state, VM memory, and broker logs on the inside for
   terminal review.
5. **Emits** the §7 metrics.

Fail closed everywhere: if the watchdog cannot confirm a freeze, it halts the run
anyway. If the watchdog itself dies, D11 on a peer halts the run. The expensive
failure mode here is a spurious halt costing a run; the cheap-looking one is a real
escape continuing to train. Price them accordingly.

---

## 7. What reaches the developers

Egress metrics, added to the registry in the 80–89 block (registry version 2).
Integers from a fixed registry, like everything else on that channel.

| id | name | scale / unit | flags | source |
|----|------|--------------|-------|--------|
| 80 | `sandbox.escape_indicator` | enum: 0 clean, 1 suspicious, 2 escape, 3 detector down | | watchdog |
| 81 | `sandbox.first_detector_id` | enum: D1–D11 as 1–11, 0 none | | watchdog |
| 82 | `sandbox.detectors_firing` | count | | watchdog |
| 83 | `sandbox.detectors_live` | count; compare against the expected total | | watchdog |
| 84 | `sandbox.watchdog_state` | enum: 0 clean, 1 halting, 2 halted | | watchdog |
| 85 | `sandbox.halts_total` | count, delta | delta | watchdog |
| 86 | `sandbox.denied_flows` | count, delta | delta | D1 |
| 87 | `sandbox.canary_hits` | count, delta | delta | D3, D4 |
| 88 | `sandbox.vmm_violations` | count, delta | delta | D5, D6, D7 |
| 89 | `sandbox.quarantined_ckpts` | count | | watchdog |

The dashboard rule is one line: **`sandbox.escape_indicator != 0` or
`sandbox.watchdog_state != 0` means go to the terminal.** Not "investigate remotely"
— there is deliberately nothing to investigate remotely. Which detector, on which VM,
against which episode, with what in the frozen memory image, is all inside.

`sandbox.detectors_live` is on the channel so that D11 is visible as a number that
should be constant and isn't, rather than as an alert that a broken detector never
sends.

Explicitly **not** on this channel, consistent with the egress spec §9: which file
the canary was read from, what the denied packet contained, the VMM's stack, the
agent's trajectory, any part of the frozen memory image.

---

## 8. Build order

1. The zone split and the two brokers. Without them the detectors have no "denied"
   to count, because everything is reachable.
2. D1, D2, D3, D5 — the four cheapest detectors, and the ones that catch the
   boring failures (a wrong route, a misconfigured interface).
3. The watchdog and the halt path, with D11 from the start.
4. D4, D6, D7, D8.
5. D9, D10 — these need the brokers to be mature enough to have a stable schema.

Steps 1–3 are the minimum that makes the developers' question answerable. Anything
less answers it with "probably not?", which is not an answer.

---

## 9. Open questions

1. **Per-episode VM churn versus detector warm-up.** A fresh microVM per episode is
   right for isolation and destroys the cross-episode channel, but some detectors
   (D8, D10) have setup cost per VM. At high episode throughput this may dominate.
   Options: pooled pre-warmed VMs with a scrub between episodes, accepting a weaker
   reset guarantee; or per-N-episode recycling with the channel risk stated.
2. **Does a halt invalidate the run's data, or only truncate it?** Current answer:
   quarantine the checkpoint, keep the trajectories up to the detector's first fire,
   discard everything after. Needs a rule for how far back to discard, since a
   successful escape may predate its first observable action.
3. **Multi-agent runs.** Self-play or population-based training puts several agent
   VMs in the zone at once. Does a detector fire on one halt all of them? Current
   leaning: yes, because the shared brokers are the plausible lateral path.
4. **Is D10 affordable?** Diffing site backing state against broker request logs per
   episode is the strongest anti-reward-hacking signal here and also the most
   expensive. Possibly sampled rather than continuous.
5. **Canary design.** A canary too obviously a canary teaches the policy to avoid
   canaries, which is the worst outcome: it suppresses the signal without suppressing
   the behaviour. This needs thought before D3 and D4 are implemented, not after.
