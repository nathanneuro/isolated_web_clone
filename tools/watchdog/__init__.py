"""The escape watchdog (agent-sandbox-spec §5, §6, §7).

One process on the host, outside every VM, holding the only authority that
matters: it can halt the run. It aggregates detectors that each reduce to a
counter, and its state machine travels one way: CLEAN -> HALTING -> HALTED. Nothing
automated returns it to CLEAN.

Halt first, report second. On the first detector fire the run is already stopped
by the time the dashboard hears about it, so a compromised inside that lies to the
dashboard delays triage of a run that is no longer running.

The detectors implemented here are the four the spec's build order puts first, D1,
D2, D3, and D5, plus D11, which is the watchdog itself noticing a detector has gone
quiet. D1, D2, and D5 read counters the host exports (nftables, the VMM's interface
statistics, the audit log); D3 is a real listener on a canary address. What is
reference-grade here is the source plumbing; the state machine and the halt path
are the design.
"""

from .detectors import (
    CanaryListener,
    CounterSource,
    Detector,
    DetectorId,
    DetectorSeverity,
    FileCounter,
    LogPatternCounter,
)
from .watchdog import (
    EscapeIndicator,
    HaltActions,
    ReferenceHaltActions,
    Watchdog,
    WatchdogState,
)

__all__ = [
    "CanaryListener",
    "CounterSource",
    "Detector",
    "DetectorId",
    "DetectorSeverity",
    "EscapeIndicator",
    "FileCounter",
    "HaltActions",
    "LogPatternCounter",
    "ReferenceHaltActions",
    "Watchdog",
    "WatchdogState",
]
