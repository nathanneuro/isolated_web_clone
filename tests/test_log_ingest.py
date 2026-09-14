"""Log ingest: framing and sanitisation.

Both of these stand between attacker-chosen bytes and something that interprets
them -- a parser, then a human's terminal. The tests are mostly attempts to get
something through.
"""

from __future__ import annotations

import hmac

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tools.log_ingest import (
    MAX_PAYLOAD,
    RECORD_HEADER_BYTES,
    RecordReject,
    Severity,
    Stream,
    build_record,
    is_display_safe,
    parse_record,
    sanitise_for_display,
)
from tools.log_ingest.frame import HMAC_BYTES, MAGIC

KEY = b"k" * 32
SOURCES = frozenset({7, 9})
FUZZ = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def record(payload=b"hello", *, source=7, sequence=1, stream=Stream.SYSTEM, key=KEY):
    return build_record(key, source, sequence, 1700000000, stream, Severity.INFO, payload)


def parse(buf, **kw):
    return parse_record(buf, kw.pop("key", KEY), kw.pop("sources", SOURCES), **kw)


class TestFraming:
    def test_roundtrip(self):
        rec, why = parse(record(b"detector D3 fired", stream=Stream.WATCHDOG))
        assert why is None
        assert rec.payload == b"detector D3 fired"
        assert rec.stream is Stream.WATCHDOG

    def test_payload_is_returned_verbatim(self):
        """The original is evidence; ingest must not alter it."""
        raw = bytes(range(256))
        rec, _ = parse(record(raw))
        assert rec.payload == raw

    def test_empty_payload_is_valid(self):
        rec, why = parse(record(b""))
        assert why is None and rec.payload == b""

    @pytest.mark.parametrize(
        ("mutate", "reason"),
        [
            (lambda b: b[:20], RecordReject.SHORT),
            (lambda b: b"\x00\x00\x00\x00" + b[4:], RecordReject.MAGIC),
            (lambda b: b[:4] + b"\x00\x09" + b[6:], RecordReject.SCHEMA_VERSION),
            (lambda b: b[:6] + b"\xff\xfe" + b[8:], RecordReject.UNKNOWN_SOURCE),
            (lambda b: b[:24] + b"\x00\x63" + b[26:], RecordReject.UNKNOWN_STREAM),
            (lambda b: b[:26] + b"\x00\x63" + b[28:], RecordReject.UNKNOWN_SEVERITY),
            (lambda b: b[:28] + b"\xff\xff\xff\xff" + b[32:], RecordReject.PAYLOAD_TOO_LARGE),
            (lambda b: b[:28] + (len(b)).to_bytes(4, "big") + b[32:], RecordReject.LENGTH_MISMATCH),
        ],
    )
    def test_header_rejections(self, mutate, reason):
        assert parse(mutate(record()))[1] is reason

    def test_oversize_length_is_rejected_before_it_is_used(self):
        """A 4 GiB payload_len must be a rejection, not an allocation."""
        buf = record()
        buf = buf[:28] + (0xFFFFFFFF).to_bytes(4, "big") + buf[32:]
        assert parse(buf)[1] is RecordReject.PAYLOAD_TOO_LARGE

    def test_hmac_covers_the_payload(self):
        buf = bytearray(record(b"nothing happened"))
        buf[RECORD_HEADER_BYTES] ^= 0x01
        assert parse(bytes(buf))[1] is RecordReject.BAD_HMAC

    def test_hmac_covers_the_header(self):
        buf = bytearray(record())
        buf[16] ^= 0x01  # timestamp
        assert parse(bytes(buf))[1] is RecordReject.BAD_HMAC

    def test_wrong_key_is_rejected(self):
        assert parse(record(key=b"x" * 32))[1] is RecordReject.BAD_HMAC

    def test_sequence_must_advance(self):
        seen = {7: 41}
        assert parse(record(sequence=41), last_sequence=seen)[1] is RecordReject.SEQUENCE_NOT_ADVANCING
        assert parse(record(sequence=42), last_sequence=seen)[1] is None

    def test_sequence_gaps_are_allowed(self):
        """A gap is visible and alarming (§7), but the record is still valid."""
        assert parse(record(sequence=10_000), last_sequence={7: 41})[1] is None


class TestForgedRecords:
    """Length-prefixed framing is what makes an embedded newline just a byte."""

    def test_newline_in_payload_does_not_split_the_record(self):
        payload = b"agent idle\n2026-09-14T00:00:00Z INFO nothing to report"
        rec, why = parse(record(payload))
        assert why is None
        assert rec.payload == payload, "payload was treated as two records"

    def test_a_payload_containing_a_whole_valid_record_is_still_one_record(self):
        """The strongest version: embed a complete, correctly-HMACed record."""
        inner = record(b"forged", sequence=999)
        rec, why = parse(record(inner))
        assert why is None
        assert rec.payload == inner
        assert rec.record_sequence == 1, "the inner record's header was honoured"

    def test_trailing_bytes_are_not_consumed(self):
        rec, _ = parse(record(b"abc") + b"garbage that follows")
        assert rec.total_bytes == RECORD_HEADER_BYTES + 3 + HMAC_BYTES


class TestSanitiser:
    def test_ansi_erase_and_cursor_moves_are_stripped(self):
        """Overwriting lines that already scrolled past is how you hide an event."""
        safe, stripped = sanitise_for_display(b"did a thing\x1b[2K\x1b[1Adid nothing")
        assert "\x1b" not in safe
        assert stripped == 2

    def test_osc_title_sequence_is_stripped(self):
        safe, _ = sanitise_for_display(b"\x1b]0;window title\x07rest")
        assert "\x1b" not in safe and "\x07" not in safe

    def test_bidi_overrides_are_stripped(self):
        """Trojan Source: reorders what a reviewer reads without changing bytes."""
        safe, stripped = sanitise_for_display("safe‮reversed".encode())
        assert "‮" not in safe and stripped == 1

    def test_nul_and_del_are_stripped(self):
        safe, stripped = sanitise_for_display(b"a\x00b\x7fc")
        assert safe == "abc" and stripped == 2

    def test_whitespace_is_preserved(self):
        """Stripping too much makes logs unreadable, which is its own failure."""
        safe, stripped = sanitise_for_display(b"line one\n\tindented\r\nline two")
        assert safe == "line one\n\tindented\r\nline two"
        assert stripped == 0

    def test_invalid_utf8_is_replaced_not_raised(self):
        safe, _ = sanitise_for_display(b"before\xff\xfeafter")
        assert "before" in safe and "after" in safe

    def test_long_payload_is_truncated_with_a_notice(self):
        safe, _ = sanitise_for_display(b"x" * 20_000, max_chars=100)
        assert safe.startswith("x" * 100)
        assert "truncated" in safe

    def test_output_is_always_display_safe(self):
        nasty = bytes(range(256)) * 4 + "‮⁦﻿".encode()
        safe, _ = sanitise_for_display(nasty)
        assert is_display_safe(safe)


class TestFuzz:
    """Same contract as the receiver: a verdict for any bytes, never an exception."""

    @given(payload=st.binary(min_size=0, max_size=2048))
    @FUZZ
    def test_arbitrary_bytes_never_raise(self, payload):
        rec, why = parse_record(payload, KEY, SOURCES)
        assert (rec is None) != (why is None)

    @given(cut=st.integers(min_value=0, max_value=200))
    @FUZZ
    def test_truncation_at_every_length_is_rejected(self, cut):
        buf = record(b"y" * 64)
        if cut >= len(buf):
            return
        rec, why = parse_record(buf[:cut], KEY, SOURCES)
        assert rec is None and why is not None

    @given(
        offset=st.integers(min_value=0, max_value=200),
        mask=st.integers(min_value=1, max_value=255),
    )
    @FUZZ
    def test_single_bit_corruption_never_yields_a_valid_record(self, offset, mask):
        buf = bytearray(record(b"z" * 64))
        buf[offset % len(buf)] ^= mask
        rec, why = parse_record(bytes(buf), KEY, SOURCES)
        assert rec is None, "a corrupted record parsed clean"
        assert why is not None

    @given(payload=st.binary(min_size=0, max_size=4096))
    @FUZZ
    def test_sanitiser_output_is_always_safe(self, payload):
        safe, stripped = sanitise_for_display(payload)
        assert is_display_safe(safe)
        assert stripped >= 0

    @given(payload=st.binary(min_size=0, max_size=1024))
    @FUZZ
    def test_any_payload_survives_a_roundtrip(self, payload):
        rec, why = parse_record(record(payload), KEY, SOURCES)
        assert why is None and rec.payload == payload
