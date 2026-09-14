"""The wire frame. Hand-written, fixed size, integers at fixed offsets (§4).

    offset  size   field
    0       4      magic            0x41474D31 ("AGM1")
    4       2      schema_version   uint16
    6       2      registry_version uint16
    8       8      frame_sequence   uint64, monotonic per sender
    16      8      timestamp        uint64
    24      2      run_id           uint16, 0 = system
    26      2      metric_count     uint16, 0..MAX_METRICS
    28      n*8    metrics          (uint16 id, uint16 flags, int32 value)
    ...     32     hmac             HMAC-SHA256 over bytes 0..(28 + n*8)

Frame size is FIXED at 28 + MAX_METRICS*8 + 32 = 572 bytes regardless of
metric_count; unused slots are zero-filled. A variable-length frame would reintroduce
a length the reader has to trust, and would leak information in its size.

There is no deserialisation library here and no code path that treats frame bytes as
anything but integers (§6). That is the whole class of parser exploits removed rather
than mitigated.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import IntEnum

MAGIC = 0x41474D31
SCHEMA_VERSION = 1
MAX_METRICS = 64
HEADER_BYTES = 28
SLOT_BYTES = 8
HMAC_BYTES = 32
FRAME_BYTES = HEADER_BYTES + MAX_METRICS * SLOT_BYTES + HMAC_BYTES  # 572

FLAG_DELTA = 1 << 0
FLAG_STALE = 1 << 1
RESERVED_FLAG_MASK = ~(FLAG_DELTA | FLAG_STALE) & 0xFFFF

INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1


class FrameReject(IntEnum):
    WRONG_SIZE = 1
    MAGIC = 2
    SCHEMA_VERSION = 3
    REGISTRY_VERSION = 4
    BAD_HMAC = 5
    SEQUENCE_NOT_ADVANCING = 6
    METRIC_COUNT = 7
    UNKNOWN_METRIC_ID = 8
    RESERVED_FLAG_SET = 9


@dataclass(frozen=True)
class MetricValue:
    metric_id: int
    value: int
    flags: int = 0

    def __post_init__(self) -> None:
        assert INT32_MIN <= self.value <= INT32_MAX, f"value {self.value} is not int32"
        assert 0 <= self.flags <= 0xFFFF
        assert not (self.flags & RESERVED_FLAG_MASK), "reserved flag bits must be zero"


@dataclass(frozen=True)
class Frame:
    registry_version: int
    frame_sequence: int
    timestamp: int
    run_id: int
    metrics: tuple[MetricValue, ...]


def build_frame(
    key: bytes,
    registry_version: int,
    frame_sequence: int,
    timestamp: int,
    run_id: int,
    metrics: list[MetricValue],
) -> bytes:
    assert len(metrics) <= MAX_METRICS, f"{len(metrics)} metrics exceeds {MAX_METRICS}"
    body = bytearray(HEADER_BYTES + MAX_METRICS * SLOT_BYTES)
    body[0:4] = MAGIC.to_bytes(4, "big")
    body[4:6] = SCHEMA_VERSION.to_bytes(2, "big")
    body[6:8] = registry_version.to_bytes(2, "big")
    body[8:16] = frame_sequence.to_bytes(8, "big")
    body[16:24] = timestamp.to_bytes(8, "big")
    body[24:26] = run_id.to_bytes(2, "big")
    body[26:28] = len(metrics).to_bytes(2, "big")
    for i, metric in enumerate(metrics):
        at = HEADER_BYTES + i * SLOT_BYTES
        body[at : at + 2] = metric.metric_id.to_bytes(2, "big")
        body[at + 2 : at + 4] = metric.flags.to_bytes(2, "big")
        body[at + 4 : at + 8] = metric.value.to_bytes(4, "big", signed=True)
    frame = bytes(body) + hmac.new(key, bytes(body), "sha256").digest()
    assert len(frame) == FRAME_BYTES
    return frame


def parse_frame(
    buf: bytes,
    key: bytes,
    registry_version: int,
    known_ids: frozenset[int],
    last_sequence: int | None = None,
) -> tuple[Frame | None, FrameReject | None]:
    """Parse one frame. Returns `(frame, None)` or `(None, reason)`. Never raises.

    Order matters: size, then magic, then versions, then HMAC, then everything that
    depends on trusting the contents. A frame that fails any check is dropped whole
    -- never partially applied -- because a half-applied frame writes a metric the
    sender did not send.
    """
    if len(buf) != FRAME_BYTES:
        return None, FrameReject.WRONG_SIZE
    if int.from_bytes(buf[0:4], "big") != MAGIC:
        return None, FrameReject.MAGIC
    if int.from_bytes(buf[4:6], "big") != SCHEMA_VERSION:
        return None, FrameReject.SCHEMA_VERSION
    if int.from_bytes(buf[6:8], "big") != registry_version:
        return None, FrameReject.REGISTRY_VERSION

    body = buf[: HEADER_BYTES + MAX_METRICS * SLOT_BYTES]
    if not hmac.compare_digest(hmac.new(key, body, "sha256").digest(), buf[-HMAC_BYTES:]):
        return None, FrameReject.BAD_HMAC

    sequence = int.from_bytes(buf[8:16], "big")
    if last_sequence is not None and sequence <= last_sequence:
        return None, FrameReject.SEQUENCE_NOT_ADVANCING

    count = int.from_bytes(buf[26:28], "big")
    if count > MAX_METRICS:
        return None, FrameReject.METRIC_COUNT

    metrics = []
    for i in range(count):
        at = HEADER_BYTES + i * SLOT_BYTES
        metric_id = int.from_bytes(buf[at : at + 2], "big")
        if metric_id not in known_ids:
            return None, FrameReject.UNKNOWN_METRIC_ID
        flags = int.from_bytes(buf[at + 2 : at + 4], "big")
        if flags & RESERVED_FLAG_MASK:
            return None, FrameReject.RESERVED_FLAG_SET
        value = int.from_bytes(buf[at + 4 : at + 8], "big", signed=True)
        metrics.append(MetricValue(metric_id, value, flags))

    return (
        Frame(
            registry_version=int.from_bytes(buf[6:8], "big"),
            frame_sequence=sequence,
            timestamp=int.from_bytes(buf[16:24], "big"),
            run_id=int.from_bytes(buf[24:26], "big"),
            metrics=tuple(metrics),
        ),
        None,
    )
