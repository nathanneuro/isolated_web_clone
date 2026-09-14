"""The outside reader (§6).

A few hundred lines with no dependency beyond an HMAC library. It reads exactly one
frame's worth of bytes, checks in order, and drops the whole frame on any failure.
Dropped-frame counters are exposed so a misbehaving sender shows up as a gap plus a
rising drop count rather than as silence.

Downstream of here, nothing ever sees wire data. Metric names, units, and scales come
from the registry compiled into this process; the only things originating inside the
airgap are an integer value and a timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .frame import FRAME_BYTES, Frame, FrameReject, parse_frame
from .registry import Registry


@dataclass
class ReaderCounters:
    frames_accepted: int = 0
    frames_dropped: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    def drop(self, reason: FrameReject) -> None:
        self.frames_dropped += 1
        self.by_reason[reason.name] = self.by_reason.get(reason.name, 0) + 1


@dataclass(frozen=True)
class Reading:
    """One decoded sample. This is what reaches the time-series store."""

    timestamp: int
    run_id: int
    name: str
    value: float | int
    delta: bool
    stale: bool


class EgressReader:
    """Parses frames and decodes them against the pinned registry."""

    def __init__(self, registry: Registry, key: bytes) -> None:
        self.registry = registry
        self._key = key
        self._known = frozenset(registry.metrics)
        self.counters = ReaderCounters()
        self.last_sequence: int | None = None
        self.readings: list[Reading] = []

    def feed(self, buf: bytes) -> list[Reading]:
        """Consume one frame's worth of bytes. Returns readings, or none."""
        frame, reason = parse_frame(
            buf, self._key, self.registry.version, self._known, self.last_sequence
        )
        if frame is None:
            self.counters.drop(reason)
            return []

        self.last_sequence = frame.frame_sequence
        self.counters.frames_accepted += 1
        readings = [
            Reading(
                timestamp=frame.timestamp,
                run_id=frame.run_id,
                name=self.registry.metrics[m.metric_id].name,
                value=self.registry.metrics[m.metric_id].decode(m.value),
                delta=bool(m.flags & 1),
                stale=bool(m.flags & 2),
            )
            for m in frame.metrics
        ]
        self.readings += readings
        return readings

    def feed_stream(self, buf: bytes) -> list[Reading]:
        """Consume as many whole frames as the buffer holds. Partial tail ignored."""
        out: list[Reading] = []
        for offset in range(0, len(buf) - FRAME_BYTES + 1, FRAME_BYTES):
            out += self.feed(buf[offset : offset + FRAME_BYTES])
        return out
