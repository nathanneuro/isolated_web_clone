"""The quarantine tier (log-diode-spec §5).

    1. framing validation  -> failures are RETAINED as evidence, never promoted
    2. sanitisation        -> a derived display copy; the original stays byte-exact
    3. malware scan        -> over the raw payload
    4. promotion           -> only records that passed everything, indexed on
                              HEADER FIELDS ONLY

The rule that shapes the code: **no path, filename, or index key is ever derived
from payload bytes.** Every location on disk comes from header integers, which are
validated against fixed registries before use. A payload that wants to be
`../../etc/cron.d/evil` has nowhere to put that string.

Failed records are kept. On the inside there is no benign explanation for a record
that fails its HMAC or trips a scanner, so the record is the finding, and discarding
it would destroy the evidence of whatever produced it.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from .frame import HMAC_BYTES, RECORD_HEADER_BYTES, LogRecord, RecordReject, parse_record
from .sanitise import sanitise_for_display


class Verdict(Enum):
    PROMOTED = "promoted"
    REJECTED_FRAMING = "rejected_framing"
    REJECTED_MALWARE = "rejected_malware"


class Scanner(Protocol):
    """A malware scanner over raw payload bytes. Returns True if clean."""

    name: str

    def __call__(self, payload: bytes) -> bool: ...


class NullScanner:
    """A scanner that scans nothing, and says so.

    There is deliberately no default scanner. A deployment that wants to run without
    one has to name this class, so "we never wired up scanning" cannot happen by
    omission -- which is how it usually happens. §5 lists scanning as defence in
    depth behind the refusal to parse, not as the boundary, but silently skipping it
    is still a decision somebody should have made on purpose.
    """

    name = "null-scanner-scans-nothing"

    def __call__(self, payload: bytes) -> bool:
        return True


@dataclass
class IngestCounters:
    """Names match schemas/metrics-registry.toml, block 90-99."""

    records_ingested: int = 0
    records_quarantined: int = 0
    framing_rejects: int = 0
    hmac_rejects: int = 0
    sequence_gaps: int = 0
    malware_findings: int = 0
    sanitiser_strips: int = 0

    def as_metrics(self) -> dict[str, int]:
        return {f"log.{k}": v for k, v in asdict(self).items()}


@dataclass
class IngestReport:
    promoted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    bytes_consumed: int = 0
    undecodable_tail: int = 0


class QuarantineTier:
    """Ingests a byte stream from the log diode into quarantine and promotion tiers.

    Layout, all of it keyed on header fields:

        <root>/quarantine/<source_id>/<sequence>.rec        byte-exact original
        <root>/quarantine/<source_id>/<sequence>.why.json   why it failed
        <root>/promoted/<stream>/<source_id>/<sequence>.rec byte-exact original
        <root>/promoted/<stream>/<source_id>/<sequence>.txt sanitised display copy
        <root>/index.jsonl                                  header fields only
    """

    def __init__(
        self,
        root: Path,
        key: bytes,
        known_sources: frozenset[int],
        scanner: Scanner,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        assert scanner is not None, (
            "pass a Scanner explicitly; use NullScanner() to run without one on purpose"
        )
        self.root = Path(root)
        self.key = key
        self.known_sources = known_sources
        self.scanner = scanner
        self._clock = clock
        self.counters = IngestCounters()
        self.last_sequence: dict[int, int] = {}
        self._highest_seen: dict[int, int] = {}
        (self.root / "quarantine").mkdir(parents=True, exist_ok=True)
        (self.root / "promoted").mkdir(parents=True, exist_ok=True)
        self._index = self.root / "index.jsonl"

    def ingest(self, stream: bytes) -> IngestReport:
        """Consume as many whole records as the buffer holds.

        Returns how many bytes were consumed so a caller can carry a partial record
        forward; a diode delivers no flow control and a record can straddle a
        delivery.
        """
        report = IngestReport()
        offset = 0
        while offset < len(stream):
            record, reason = parse_record(
                stream[offset:], self.key, self.known_sources, self.last_sequence
            )
            if record is None:
                if reason in (RecordReject.SHORT, RecordReject.LENGTH_MISMATCH):
                    # Possibly a straddling record; leave it for the next delivery.
                    report.undecodable_tail = len(stream) - offset
                    break
                consumed = self._reject_unparsed(stream, offset, reason, report)
                offset += consumed
                continue

            offset += record.total_bytes
            self._admit(record, report)

        report.bytes_consumed = offset
        return report

    def _reject_unparsed(
        self, stream: bytes, offset: int, reason: RecordReject, report: IngestReport
    ) -> int:
        """Retain a record we could not validate, then resynchronise.

        We do not know its true length (the length field is not trustworthy on a
        record that failed), so we retain the header-sized slice and skip one byte.
        Slow on a corrupt stream, and correct, which is the right trade for a path
        that should never fire.
        """
        self.counters.framing_rejects += 1
        if reason is RecordReject.BAD_HMAC:
            self.counters.hmac_rejects += 1
        self.counters.records_quarantined += 1

        blob = stream[offset : offset + RECORD_HEADER_BYTES + HMAC_BYTES]
        target = self.root / "quarantine" / "unattributed"
        target.mkdir(parents=True, exist_ok=True)
        name = f"{self._clock():.6f}-{reason.name}"
        (target / f"{name}.rec").write_bytes(blob)
        (target / f"{name}.why.json").write_text(
            json.dumps({"reject": reason.name, "code": int(reason)}, indent=1)
        )
        report.rejected.append(f"unattributed/{name}")
        return 1

    def _admit(self, record: LogRecord, report: IngestReport) -> None:
        self.counters.records_ingested += 1

        previous = self._highest_seen.get(record.source_id)
        if previous is not None and record.record_sequence > previous + 1:
            # §7: a compromised eval cluster cannot delete what has crossed, but it
            # can stop sending. A gap is how truncation looks from outside.
            self.counters.sequence_gaps += record.record_sequence - previous - 1
        self._highest_seen[record.source_id] = record.record_sequence
        self.last_sequence[record.source_id] = record.record_sequence

        if not self.scanner(record.payload):
            self.counters.malware_findings += 1
            self.counters.records_quarantined += 1
            target = self.root / "quarantine" / str(record.source_id)
            target.mkdir(parents=True, exist_ok=True)
            stem = str(record.record_sequence)
            (target / f"{stem}.rec").write_bytes(record.payload)
            (target / f"{stem}.why.json").write_text(
                json.dumps(
                    {"reject": "MALWARE", "scanner": self.scanner.name,
                     "stream": record.stream.name}, indent=1
                )
            )
            report.rejected.append(f"{record.source_id}/{stem}")
            return

        safe, stripped = sanitise_for_display(record.payload)
        self.counters.sanitiser_strips += stripped

        target = self.root / "promoted" / record.stream.name.lower() / str(record.source_id)
        target.mkdir(parents=True, exist_ok=True)
        stem = str(record.record_sequence)
        (target / f"{stem}.rec").write_bytes(record.payload)
        (target / f"{stem}.txt").write_text(safe)

        with self._index.open("a") as fh:
            fh.write(
                json.dumps(
                    {
                        "source_id": record.source_id,
                        "sequence": record.record_sequence,
                        "timestamp": record.timestamp,
                        "stream": record.stream.name,
                        "severity": record.severity.name,
                        "payload_bytes": len(record.payload),
                        "stripped": stripped,
                    }
                )
                + "\n"
            )
        report.promoted.append(f"{record.stream.name.lower()}/{record.source_id}/{stem}")
