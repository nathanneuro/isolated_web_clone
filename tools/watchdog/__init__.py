"""The escape watchdog (agent-sandbox-spec §5, §6, §7).

One process on the host, outside every VM, holding the only authority that
matters: it can halt the run. It aggregates detectors that each reduce to a
counter, and its state machine travels one way: CLEAN -> HALTING -> HALTED. Nothing
automated returns it to CLEAN.

Halt first, report second. On the first detector fire the run is already stopped
by the time the dashboard hears about it, so a compromised inside that lies to the
dashboard delays triage of a run that is no longer running.

Detectors: D1, D2, D5, D6, and D7 read counters the host exports (nftables, the
VMM's interface statistics, the audit log) through CounterSource, FileCounter, and
LogPatternCounter. D3 is a real listener on a canary address. D4 counts
presentations of a planted credential at either broker. D8 hashes the files that
must not change. D9 counts requests that did not fit a broker's schema. D10
compares the rows a site attributes to the agent with the writes the env broker
performed. D11 is the watchdog itself noticing a detector has gone quiet. What is
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
    FileIntegrityDetector,
    LogPatternCounter,
    StateBypassDetector,
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
    "FileIntegrityDetector",
    "HaltActions",
    "LogPatternCounter",
    "ReferenceHaltActions",
    "StateBypassDetector",
    "Watchdog",
    "WatchdogState",
]
