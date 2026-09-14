"""The inside sender (§5).

Reads the latest value per metric from a ring buffer, selects what fits in one
frame, HMACs it, emits it. One frame per tick, never more.

Selection is by declared priority, not by metric ID. Choosing the lowest IDs would
starve whichever block was registered last, and blocks are appended, so the newest
subsystem would always be the one that goes dark -- which is backwards, since the
newest blocks here are the sandbox watchdog and the log channel's liveness.

Nothing in this file is a security boundary. The sender runs inside; a compromised
inside owns it. These are correctness measures that keep honest components from
flooding the link. The boundary is the hardware filter upstream
(physical-controls-spec §3).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .frame import FLAG_DELTA, FLAG_STALE, MAX_METRICS, MetricValue, build_frame
from .registry import Registry


@dataclass
class SenderCounters:
    frames_sent: int = 0
    dropped_metrics: int = 0
    bad_writes: int = 0
    clamped_values: int = 0


class EgressSender:
    """Accepts (metric_id, value) pairs and emits one frame per tick."""

    def __init__(self, registry: Registry, key: bytes, run_id: int = 0) -> None:
        self.registry = registry
        self._key = key
        self.run_id = run_id
        self.counters = SenderCounters()
        self._latest: dict[int, int] = {}
        self._fresh: set[int] = set()
        self._sequence = 0
        self._rotate_cursor = 0

    def write(self, metric_id: int, value: int) -> bool:
        """Record a value. Returns False and counts if the write is not admissible.

        The socket accepts only this pair shape (§5). There is no way for a
        component to introduce a metric ID the registry does not already hold.
        """
        if metric_id not in self.registry.metrics:
            self.counters.bad_writes += 1
            return False
        if not isinstance(value, int) or isinstance(value, bool):
            self.counters.bad_writes += 1
            return False
        clamped = max(-(2**31), min(2**31 - 1, value))
        if clamped != value:
            # Saturate rather than wrap. A wrapped counter reads as a plausible
            # small number, which is worse than an obvious ceiling.
            self.counters.clamped_values += 1
        self._latest[metric_id] = clamped
        self._fresh.add(metric_id)
        return True

    def write_named(self, name: str, value: int) -> bool:
        return self.write(self.registry.by_name(name).id, value)

    def tick(self, timestamp: int) -> bytes:
        """Build exactly one frame."""
        self._sequence += 1
        selected = self._select()
        metrics = [
            MetricValue(
                metric.id,
                self._latest.get(metric.id, 0),
                (FLAG_DELTA if metric.delta else 0)
                | (0 if metric.id in self._fresh else FLAG_STALE),
            )
            for metric in selected
        ]
        self._fresh.clear()
        self.counters.frames_sent += 1
        return build_frame(
            self._key,
            self.registry.version,
            self._sequence,
            timestamp,
            self.run_id,
            metrics,
        )

    def _select(self):
        """Always-metrics every frame; the rest round-robin at a bounded interval."""
        always = self.registry.always
        free = MAX_METRICS - len(always)
        if free <= 0:
            self.counters.dropped_metrics += len(self.registry.rotating)
            return always[:MAX_METRICS]

        rotating = self.registry.rotating
        if not rotating:
            return always

        chosen = [
            rotating[(self._rotate_cursor + i) % len(rotating)]
            for i in range(min(free, len(rotating)))
        ]
        self._rotate_cursor = (self._rotate_cursor + len(chosen)) % len(rotating)
        return always + chosen
