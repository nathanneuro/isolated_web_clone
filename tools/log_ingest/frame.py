"""The record parser. Hand-written, integers at fixed offsets, no library.

    offset  size   field
    0       4      magic            0x414c4731 ("ALG1")
    4       2      schema_version   uint16
    6       2      source_id        uint16, fixed registry
    8       8      record_sequence  uint64, monotonic per source
    16      8      timestamp        uint64
    24      2      stream_id        uint16, enum
    26      2      severity         uint16, enum
    28      4      payload_len      uint32, <= MAX_PAYLOAD
    32      n      payload          OPAQUE. never parsed here.
    32+n    32     hmac             HMAC-SHA256 over bytes 0..(32+n)

Length-prefixed framing is the defence against forged records. If records were
newline-separated, a payload containing a newline and a plausible timestamp would
manufacture a log line that never happened, or split a real one so the event scrolls
away. Here a newline inside a payload is just a byte, because the only thing that
says where a record ends is a length the HMAC covers.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import IntEnum

MAGIC = 0x414C4731
SCHEMA_VERSION = 1
RECORD_HEADER_BYTES = 32
HMAC_BYTES = 32
MAX_PAYLOAD = 1024 * 1024


class Stream(IntEnum):
    TRAJECTORY = 0
    GOLIVE = 1
    WORKER = 2
    DRIVER = 3
    WATCHDOG = 4
    RUNNER = 5
    SYSTEM = 6


class Severity(IntEnum):
    DEBUG = 0
    INFO = 1
    WARN = 2
    ERROR = 3
    CRITICAL = 4


class RecordReject(IntEnum):
    """Why a record did not pass. Integers, for the egress channel."""

    SHORT = 1
    MAGIC = 2
    SCHEMA_VERSION = 3
    UNKNOWN_SOURCE = 4
    UNKNOWN_STREAM = 5
    UNKNOWN_SEVERITY = 6
    PAYLOAD_TOO_LARGE = 7
    LENGTH_MISMATCH = 8
    BAD_HMAC = 9
    SEQUENCE_NOT_ADVANCING = 10


@dataclass(frozen=True)
class LogRecord:
    """A validated record. `payload` is still untrusted bytes."""

    source_id: int
    record_sequence: int
    timestamp: int
    stream: Stream
    severity: Severity
    payload: bytes

    @property
    def total_bytes(self) -> int:
        return RECORD_HEADER_BYTES + len(self.payload) + HMAC_BYTES


def _u16(buf: bytes, offset: int) -> int:
    return (buf[offset] << 8) | buf[offset + 1]


def _u32(buf: bytes, offset: int) -> int:
    return int.from_bytes(buf[offset : offset + 4], "big")


def _u64(buf: bytes, offset: int) -> int:
    return int.from_bytes(buf[offset : offset + 8], "big")


def parse_record(
    buf: bytes,
    key: bytes,
    known_sources: frozenset[int],
    last_sequence: dict[int, int] | None = None,
) -> tuple[LogRecord | None, RecordReject | None]:
    """Parse one record from the front of `buf`.

    Returns `(record, None)` or `(None, reason)`. Never raises, never parses the
    payload, and never uses a payload byte in any decision.
    """
    if len(buf) < RECORD_HEADER_BYTES + HMAC_BYTES:
        return None, RecordReject.SHORT
    if _u32(buf, 0) != MAGIC:
        return None, RecordReject.MAGIC
    if _u16(buf, 4) != SCHEMA_VERSION:
        return None, RecordReject.SCHEMA_VERSION

    source_id = _u16(buf, 6)
    if source_id not in known_sources:
        return None, RecordReject.UNKNOWN_SOURCE

    payload_len = _u32(buf, 28)
    if payload_len > MAX_PAYLOAD:
        # Checked before it is used as a length, so a huge value is a rejection
        # rather than an allocation.
        return None, RecordReject.PAYLOAD_TOO_LARGE
    end = RECORD_HEADER_BYTES + payload_len
    if len(buf) < end + HMAC_BYTES:
        return None, RecordReject.LENGTH_MISMATCH

    try:
        stream = Stream(_u16(buf, 24))
    except ValueError:
        return None, RecordReject.UNKNOWN_STREAM
    try:
        severity = Severity(_u16(buf, 26))
    except ValueError:
        return None, RecordReject.UNKNOWN_SEVERITY

    expected = hmac.new(key, buf[:end], "sha256").digest()
    if not hmac.compare_digest(expected, buf[end : end + HMAC_BYTES]):
        return None, RecordReject.BAD_HMAC

    sequence = _u64(buf, 8)
    if last_sequence is not None:
        previous = last_sequence.get(source_id)
        if previous is not None and sequence <= previous:
            return None, RecordReject.SEQUENCE_NOT_ADVANCING

    return (
        LogRecord(
            source_id=source_id,
            record_sequence=sequence,
            timestamp=_u64(buf, 16),
            stream=stream,
            severity=severity,
            payload=buf[RECORD_HEADER_BYTES : end],
        ),
        None,
    )


def build_record(
    key: bytes,
    source_id: int,
    record_sequence: int,
    timestamp: int,
    stream: Stream,
    severity: Severity,
    payload: bytes,
) -> bytes:
    """Build a record. Used by the eval-side writer and by the tests."""
    assert len(payload) <= MAX_PAYLOAD, "payload exceeds cap"
    header = (
        MAGIC.to_bytes(4, "big")
        + SCHEMA_VERSION.to_bytes(2, "big")
        + source_id.to_bytes(2, "big")
        + record_sequence.to_bytes(8, "big")
        + timestamp.to_bytes(8, "big")
        + int(stream).to_bytes(2, "big")
        + int(severity).to_bytes(2, "big")
        + len(payload).to_bytes(4, "big")
    )
    body = header + payload
    return body + hmac.new(key, body, "sha256").digest()
