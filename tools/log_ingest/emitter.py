"""The eval-side record writer (log-diode-spec §4), and the source registry.

Every component inside that has something to say beyond a number says it here:
a record with an integer header and an opaque payload, HMAC'd, sequenced per
source, spooled as one file per record for the log diode to move. The writer
never reads back, never deletes, and cannot learn whether anything arrived.

Sources are a fixed registry, like metrics. A record from an unknown source is
rejected at ingest; there is no way for a new component to start talking without
a release on both ends.
"""

from __future__ import annotations

import json
from enum import IntEnum
from pathlib import Path
from typing import Callable

from .frame import Severity, Stream, build_record
from .quarantine import IngestReport, QuarantineTier


class LogSource(IntEnum):
    """`source_id` values. Never reused."""

    EVAL_LOOP = 1
    WORKER = 2
    GOLIVE = 3
    DRIVER = 4
    WATCHDOG = 5
    RECEIVER = 6
    COMMAND_EXECUTOR = 7


KNOWN_SOURCES = frozenset(int(s) for s in LogSource)


class LogEmitter:
    def __init__(self, key: bytes, source: LogSource, spool: Path, *, clock: Callable[[], float]) -> None:
        self.source = LogSource(source)
        self._key = key
        self.spool = Path(spool)
        self.spool.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self.sequence = 0

    def emit(self, stream: Stream, severity: Severity, payload: bytes) -> Path:
        self.sequence += 1
        record = build_record(
            self._key, int(self.source), self.sequence, int(self._clock()), stream, severity, payload
        )
        path = self.spool / f"{int(self.source):04d}-{self.sequence:012d}.rec"
        path.write_bytes(record)
        return path

    def emit_json(self, stream: Stream, severity: Severity, document: object) -> Path:
        """Convenience for structured payloads. Still opaque bytes on the far side."""
        return self.emit(stream, severity, json.dumps(document, sort_keys=True).encode())


def ingest_directory(tier: QuarantineTier, directory: Path) -> IngestReport:
    """Feed every delivered record file to the quarantine tier, in name order.

    Delivery order is not guaranteed by a diode; name order is sequence order per
    source, which is what makes a missing file show up as a sequence gap.
    """
    total = IngestReport()
    for path in sorted(Path(directory).glob("*.rec")):
        report = tier.ingest(path.read_bytes())
        total.promoted += report.promoted
        total.rejected += report.rejected
        total.bytes_consumed += report.bytes_consumed
        path.rename(path.with_suffix(".done"))
    return total
