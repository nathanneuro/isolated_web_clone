"""The numeric egress channel: sender (inside) and reader (outside).

egress-metrics-spec §4-§6. Fixed-size frames of integers from a pinned registry,
a hand-written parser with no deserialiser, and a bandwidth ceiling stated rather
than hoped for.

The sender's rate cap and schema checks are correctness measures, not security
boundaries -- the sender runs inside and a compromised inside owns the process
implementing them. The boundary is the FPGA filter upstream of the link
(physical-controls-spec §3) and the reader's refusal to interpret anything that does
not match. This module is the software half of a design whose guarantees are
hardware.
"""

from .frame import FRAME_BYTES, MAX_METRICS, Frame, FrameReject, MetricValue, build_frame, parse_frame
from .reader import EgressReader, ReaderCounters
from .registry import Registry, load_registry
from .sender import EgressSender

__all__ = [
    "FRAME_BYTES",
    "MAX_METRICS",
    "EgressReader",
    "EgressSender",
    "Frame",
    "FrameReject",
    "MetricValue",
    "ReaderCounters",
    "Registry",
    "build_frame",
    "load_registry",
    "parse_frame",
]
