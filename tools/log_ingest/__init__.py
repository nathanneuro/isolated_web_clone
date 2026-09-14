"""Log ingest on the logging cluster: the far side of the log diode.

Implements log-diode-spec §4 and §5. Every byte arriving here is
attacker-influenced -- these records carry agent output, model completions, and
scraped content reproduced inside tracebacks. A log line is frequently not a report
*about* untrusted data; it is untrusted data with a timestamp on it.

So nothing here parses a payload, interpolates it, deserialises it, or lets it name
a file. Records are framed, validated on header fields only, sanitised into a
separate display copy, and held in quarantine until checked.
"""

from .frame import MAX_PAYLOAD, RECORD_HEADER_BYTES, LogRecord, RecordReject, parse_record
from .frame import Severity, Stream, build_record
from .sanitise import is_display_safe, sanitise_for_display

__all__ = [
    "MAX_PAYLOAD",
    "RECORD_HEADER_BYTES",
    "LogRecord",
    "RecordReject",
    "Severity",
    "Stream",
    "build_record",
    "is_display_safe",
    "parse_record",
    "sanitise_for_display",
]
