"""The metrics socket (§5) and what sits at each end of the link.

`Telemetry` is the socket: inside components attach as sources, and one tick
collects every source's `as_metrics()` into the sender and emits one frame. A
source that names a metric the registry does not hold is counted as a bad write
and otherwise ignored; there is no path by which a component adds a metric.

`FrameSpool` is where frames go on the transmit side: one file per frame, which
is the unit the diode moves. `ReadingStore` is the dev-side time-series store the
reader feeds, kept as a JSONL file plus the latest value per name, which is all a
dashboard rule like "escape_indicator != 0" needs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Protocol

from .reader import Reading
from .sender import EgressSender


class MetricSource(Protocol):
    def as_metrics(self) -> dict[str, int]: ...


class Telemetry:
    def __init__(self, sender: EgressSender) -> None:
        self.sender = sender
        self._sources: list[tuple[str, Callable[[], dict[str, int]]]] = []

    def attach(self, name: str, source: MetricSource | Callable[[], dict[str, int]]) -> None:
        read = source.as_metrics if hasattr(source, "as_metrics") else source
        assert callable(read), f"{name} is not a metric source"
        self._sources.append((name, read))

    @property
    def sources(self) -> list[str]:
        return [name for name, _ in self._sources]

    def collect(self) -> int:
        """Read every source into the sender. Returns how many writes were refused."""
        refused = 0
        for _, read in self._sources:
            for metric_name, value in read().items():
                try:
                    metric = self.sender.registry.by_name(metric_name)
                except KeyError:
                    self.sender.counters.bad_writes += 1
                    refused += 1
                    continue
                if not self.sender.write(metric.id, value):
                    refused += 1
        return refused

    def tick(self, timestamp: int) -> bytes:
        self.collect()
        # The sender reports on itself in the same frame, after everything else.
        self.sender.write_named("sys.heartbeat", 1)
        self.sender.write_named("egress.bad_writes", self.sender.counters.bad_writes)
        self.sender.write_named("egress.dropped_metrics", self.sender.counters.dropped_metrics)
        return self.sender.tick(timestamp)


class FrameSpool:
    """Transmit-side spool. One frame per file, named so the diode delivers in order."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._n = 0

    def put(self, frame: bytes) -> Path:
        self._n += 1
        path = self.directory / f"frame-{self._n:08d}.bin"
        path.write_bytes(frame)
        return path


class ReadingStore:
    """Dev-side store. Latest value per metric, and every reading appended to JSONL."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.latest: dict[str, Reading] = {}

    def write(self, readings: list[Reading]) -> None:
        with self.path.open("a") as fh:
            for reading in readings:
                if not reading.stale or reading.name not in self.latest:
                    self.latest[reading.name] = reading
                fh.write(json.dumps(vars(reading)) + "\n")

    def value(self, name: str):
        return self.latest[name].value

    def go_to_the_terminal(self) -> bool:
        """The dashboard rule (agent-sandbox-spec §7), verbatim."""
        return (
            self.latest.get("sandbox.escape_indicator") is not None
            and self.value("sandbox.escape_indicator") != 0
        ) or (
            self.latest.get("sandbox.watchdog_state") is not None
            and self.value("sandbox.watchdog_state") != 0
        )
