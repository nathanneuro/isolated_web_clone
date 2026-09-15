"""Which side of the diode each run-directory entry belongs to.

Both demos run every zone on one host, so a run directory mixes files that in a
real deployment would live on different machines with no path between them.
`write_layout` writes a LAYOUT.md into the run directory naming the zone of each
entry, and refuses to describe a run directory holding something it does not
know: a new directory needs a row here before it can ship.
"""

from __future__ import annotations

from pathlib import Path

ZONES = {
    "OUTSIDE": "internet-connected; runs the LLM reconstruction agents and signs bundles",
    "DIODE": "the simulated one-way link; a directory copy standing in for hardware",
    "INSIDE": "airgapped; no LLM here reads content",
    "DEV SIDE": "LAN-only reader host at the far end of the egress diode",
    "LOGGING CLUSTER": "far end of the log diode; nothing leaves it uncleaned",
    "BOTH": "held in one place only because this is a single-host demo",
}

# name -> (zone, what it holds). Order is the order LAYOUT.md lists them.
ENTRIES: dict[str, tuple[str, str]] = {
    "keys": ("BOTH", "every signing, verify, and wrapping key; a real deployment never has these on one host"),
    "outbox": ("OUTSIDE", "bundles as bundle-build wrote them: encrypted content, plaintext structure, manifest, signature"),
    "eval-package": ("OUTSIDE", "the researcher's choreography package before bundle-build sealed it"),
    "diode-transmit": ("DIODE", "ingress: archives handed to the link; nothing here is acknowledged"),
    "inbox": ("INSIDE", "ingress: archives as they arrived; the receiver's only input"),
    "state": ("INSIDE", "receiver: sequence high-water marks, rotated keys, the unpack area, and inboxes it fills"),
    "quarantine": ("INSIDE", "receiver: bundles that failed verification, kept as-is for a human at the terminal"),
    "worker-inbox": ("INSIDE", "verified site bundles waiting for the worker"),
    "command-inbox": ("INSIDE", "verified command bundles waiting for the executor"),
    "eval-inbox": ("INSIDE", "verified eval bundles waiting for eval intake"),
    "work": ("INSIDE", "worker: composed apps and go-live results, one directory per bundle"),
    "sandboxes": ("INSIDE", "go-live: decrypted, serving copies of each site; the only place plaintext content exists inside"),
    "registry.json": ("INSIDE", "site registry: hostname -> live bundle"),
    "eval-definitions.json": ("INSIDE", "eval intake: question id -> filed choreography sandbox"),
    "episodes": ("INSIDE", "per-episode site database copies and the agent's home directory"),
    "inspect": ("INSIDE", "Inspect AI eval logs for the run"),
    "watchdog-preserve": ("INSIDE", "what the halt actions checkpointed when the canary tripped"),
    "egress-transmit": ("INSIDE", "metrics frames the sender handed to the egress diode"),
    "egress-receive": ("DEV SIDE", "the same frames as the reader saw them; the reader parses nothing else"),
    "dev-dashboard": ("DEV SIDE", "readings.jsonl: metric id, value, time; the dashboard's whole input"),
    "log-transmit": ("INSIDE", "log records the emitters spooled for the log diode"),
    "log-receive": ("LOGGING CLUSTER", "log records as they arrived, before the quarantine tier"),
    "logging-cluster": ("LOGGING CLUSTER", "quarantine and promoted tiers"),
    "metadata.json": ("BOTH", "what this run did, written by the demo script itself"),
    "LAYOUT.md": ("BOTH", "this file"),
}


def write_layout(run_dir: Path) -> Path:
    present = sorted(p.name for p in run_dir.iterdir())
    unknown = [n for n in present if n not in ENTRIES]
    assert not unknown, f"run directory holds entries with no layout row: {unknown}"
    lines = [
        f"# {run_dir.name}",
        "",
        "Every zone ran on this one host. In a deployment the zones below are separate",
        "machines and the DIODE rows are hardware with no return path. Nothing under",
        "INSIDE can be read from OUTSIDE or DEV SIDE except through the numbered stages",
        "the demo printed.",
        "",
    ]
    for zone, meaning in ZONES.items():
        rows = [(n, ENTRIES[n][1]) for n in ENTRIES if ENTRIES[n][0] == zone and n in present]
        if not rows:
            continue
        lines += [f"## {zone}", "", meaning, "", "| entry | holds |", "|---|---|"]
        lines += [f"| `{n}` | {what} |" for n, what in rows]
        lines.append("")
    path = run_dir / "LAYOUT.md"
    path.write_text("\n".join(lines))
    return path


def print_layout(run_dir: Path) -> None:
    present = {p.name for p in run_dir.iterdir()}
    for zone in ZONES:
        names = [n for n in ENTRIES if ENTRIES[n][0] == zone and n in present and n != "LAYOUT.md"]
        if names:
            print(f"    {zone:16s} {', '.join(names)}")
