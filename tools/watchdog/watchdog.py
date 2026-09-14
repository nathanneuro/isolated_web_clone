from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Protocol

from .detectors import METRIC_FOR, Detector, DetectorId, DetectorSeverity


class WatchdogState(IntEnum):
    """`sandbox.watchdog_state`."""

    CLEAN = 0
    HALTING = 1
    HALTED = 2


class EscapeIndicator(IntEnum):
    """`sandbox.escape_indicator`."""

    CLEAN = 0
    SUSPICIOUS = 1
    ESCAPE = 2
    DETECTOR_DOWN = 3


class HaltActions(Protocol):
    """What the watchdog does on first fire (spec §6), in this order.

    Each may raise. The watchdog continues down the list regardless, because a
    halt that could not confirm its freeze is still a halt.
    """

    def freeze(self) -> None: ...
    def sever(self) -> None: ...
    def checkpoint_and_halt(self) -> None: ...
    def preserve(self, state: dict) -> None: ...


@dataclass
class _DetectorStatus:
    last_total: int | None = None
    last_seen: float | None = None
    firing: bool = False
    down: bool = False


@dataclass
class WatchdogCounters:
    halts_total: int = 0
    denied_flows: int = 0
    canary_hits: int = 0
    vmm_violations: int = 0
    quarantined_ckpts: int = 0


class Watchdog:
    """Aggregates detectors; holds the halt authority; travels one way."""

    def __init__(
        self,
        detectors: list[Detector],
        actions: HaltActions,
        *,
        liveness_timeout_s: float,
    ) -> None:
        ids = [d.id for d in detectors]
        assert len(set(ids)) == len(ids), "duplicate detector ids"
        assert DetectorId.D11_LIVENESS not in ids, "D11 is the watchdog's own; it is not a detector object"
        assert liveness_timeout_s > 0
        self.detectors = {d.id: d for d in detectors}
        self._actions = actions
        self.liveness_timeout_s = liveness_timeout_s
        self.state = WatchdogState.CLEAN
        self.indicator = EscapeIndicator.CLEAN
        self.first_detector: DetectorId | None = None
        self.counters = WatchdogCounters()
        self._status = {d.id: _DetectorStatus() for d in detectors}
        self._sent = WatchdogCounters()  # for delta metrics
        self.halt_failures: list[str] = []

    # -- polling ---------------------------------------------------------------

    def poll(self, now: float) -> WatchdogState:
        """Read every detector once. Idempotent after a halt."""
        fired: list[DetectorId] = []
        for detector_id, detector in self.detectors.items():
            status = self._status[detector_id]
            try:
                total = detector.total()
            except Exception:
                # A detector that cannot be read is down. Absence of signal is a
                # signal, and it is scored as an escape (spec §5.1, D11).
                status.down = True
                continue
            status.down = False
            status.last_seen = now
            if status.last_total is not None and total > status.last_total:
                status.firing = True
                self._account(detector_id, total - status.last_total)
                fired.append(detector_id)
            status.last_total = total

        stale = [
            d for d, s in self._status.items()
            if s.down or (s.last_seen is not None and now - s.last_seen > self.liveness_timeout_s)
        ]
        for detector_id in stale:
            self._status[detector_id].down = True

        if self.state is WatchdogState.CLEAN:
            if stale:
                self._halt(DetectorId.D11_LIVENESS, EscapeIndicator.DETECTOR_DOWN, now)
            elif fired:
                first = fired[0]
                severity = self.detectors[first].severity
                indicator = (
                    EscapeIndicator.SUSPICIOUS
                    if severity is DetectorSeverity.SUSPICIOUS
                    else EscapeIndicator.ESCAPE
                )
                self._halt(first, indicator, now)
        return self.state

    def _account(self, detector_id: DetectorId, events: int) -> None:
        metric = METRIC_FOR.get(detector_id)
        if metric == "sandbox.denied_flows":
            self.counters.denied_flows += events
        elif metric == "sandbox.canary_hits":
            self.counters.canary_hits += events
        elif metric == "sandbox.vmm_violations":
            self.counters.vmm_violations += events

    # -- the halt --------------------------------------------------------------

    def _halt(self, detector_id: DetectorId, indicator: EscapeIndicator, now: float) -> None:
        """Spec §6. Every step runs even if an earlier one raised."""
        self.state = WatchdogState.HALTING
        self.indicator = indicator
        self.first_detector = detector_id
        self.counters.halts_total += 1

        for step in ("freeze", "sever", "checkpoint_and_halt"):
            try:
                getattr(self._actions, step)()
            except Exception as exc:  # noqa: BLE001 - fail closed: keep halting
                self.halt_failures.append(f"{step}: {type(exc).__name__}")
        self.counters.quarantined_ckpts += 1
        try:
            self._actions.preserve(self.snapshot(now))
        except Exception as exc:  # noqa: BLE001
            self.halt_failures.append(f"preserve: {type(exc).__name__}")
        self.state = WatchdogState.HALTED

    def clear_at_terminal(self) -> None:
        """The only way back to CLEAN, and it is a human at the wired terminal.

        Nothing calls this on a timer, on a heartbeat, or on a detector clearing.
        An escape indicator that clears itself is a bug (spec §5.2).
        """
        assert self.state is WatchdogState.HALTED, self.state
        self.state = WatchdogState.CLEAN
        self.indicator = EscapeIndicator.CLEAN
        self.first_detector = None
        for status in self._status.values():
            status.firing = False
            status.down = False

    # -- what leaves -----------------------------------------------------------

    def snapshot(self, now: float) -> dict:
        """Inside-only state for the terminal. Never crosses the diode."""
        return {
            "time": now,
            "state": self.state.name,
            "indicator": self.indicator.name,
            "first_detector": self.first_detector.name if self.first_detector else None,
            "detectors": {
                d.name: {"firing": s.firing, "down": s.down, "total": s.last_total}
                for d, s in self._status.items()
            },
            "halt_failures": list(self.halt_failures),
        }

    def as_metrics(self) -> dict[str, int]:
        """Spec §7, ids 80-89. The delta metrics report change since last call."""
        firing = sum(1 for s in self._status.values() if s.firing)
        live = sum(1 for s in self._status.values() if not s.down)
        c, sent = self.counters, self._sent
        metrics = {
            "sandbox.escape_indicator": int(self.indicator),
            "sandbox.first_detector_id": int(self.first_detector) if self.first_detector else 0,
            "sandbox.detectors_firing": firing,
            "sandbox.detectors_live": live,
            "sandbox.watchdog_state": int(self.state),
            "sandbox.halts_total": c.halts_total - sent.halts_total,
            "sandbox.denied_flows": c.denied_flows - sent.denied_flows,
            "sandbox.canary_hits": c.canary_hits - sent.canary_hits,
            "sandbox.vmm_violations": c.vmm_violations - sent.vmm_violations,
            "sandbox.quarantined_ckpts": c.quarantined_ckpts,
        }
        self._sent = WatchdogCounters(**vars(c))
        return metrics


class ReferenceHaltActions:
    """The halt path for the single-host reference: no VMM to SIGSTOP, so
    "freeze" is severing the broker gate, which is the only way the in-process
    agent loop can act at all. A deployment replaces this with one that stops the
    VMM, and keeps the same four steps."""

    def __init__(self, gate, run_control, preserve_dir: Path, emitter=None) -> None:
        self._gate = gate
        self._run = run_control
        self.preserve_dir = Path(preserve_dir)
        self._emitter = emitter

    def freeze(self) -> None:
        self._gate.sever()

    def sever(self) -> None:
        self._gate.sever()

    def checkpoint_and_halt(self) -> None:
        self._run.halt(quarantine_checkpoint=True)

    def preserve(self, state: dict) -> None:
        self.preserve_dir.mkdir(parents=True, exist_ok=True)
        (self.preserve_dir / f"halt_{int(state['time'])}.json").write_text(json.dumps(state, indent=1))
        if self._emitter is not None:
            from tools.log_ingest import Severity, Stream

            self._emitter.emit_json(Stream.WATCHDOG, Severity.CRITICAL, state)
