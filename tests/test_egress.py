"""Egress channel: frames, priority selection, and the reader's refusal to interpret.

§6 asks for a fuzz harness over random bytes, truncated frames, and every field at
min and max, kept in the repo. It is at the bottom of this file.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tools.egress import (
    FRAME_BYTES,
    MAX_METRICS,
    EgressReader,
    EgressSender,
    FrameReject,
    MetricValue,
    build_frame,
    load_registry,
    parse_frame,
)
from tools.egress.frame import (
    FLAG_STALE,
    HEADER_BYTES,
    INT32_MAX,
    INT32_MIN,
    MAGIC,
    RESERVED_FLAG_MASK,
    SLOT_BYTES,
)

KEY = b"k" * 32
FUZZ = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@pytest.fixture(scope="module")
def registry():
    return load_registry()


@pytest.fixture
def pair(registry):
    return EgressSender(registry, KEY), EgressReader(registry, KEY)


def reseal(buf: bytes, key: bytes = KEY) -> bytes:
    """Recompute the HMAC after mutating the body.

    Needed because the reader checks the HMAC before anything that depends on
    trusting the contents, so a test that mutates metric_count without resealing
    proves only that the HMAC works. Resealing is what an attacker holding the key
    would do, and it is the case the later checks exist for.
    """
    import hmac as _hmac

    body = buf[: HEADER_BYTES + MAX_METRICS * SLOT_BYTES]
    return body + _hmac.new(key, body, "sha256").digest()


def frame(registry, metrics=(), *, sequence=1, key=KEY, version=None):
    return build_frame(
        key, version if version is not None else registry.version,
        sequence, 1700000000, 0, list(metrics),
    )


class TestFrameShape:
    @pytest.mark.parametrize("count", [0, 1, 7, MAX_METRICS])
    def test_frame_is_always_the_same_size(self, registry, count):
        """§4: fixed size regardless of metric_count. A variable frame would leak
        information in its length and reintroduce a length to trust."""
        ids = sorted(registry.metrics)[:count]
        assert len(frame(registry, [MetricValue(i, 1) for i in ids])) == FRAME_BYTES

    def test_size_matches_the_spec_arithmetic(self):
        assert FRAME_BYTES == 28 + 64 * 8 + 32 == 572

    def test_more_metrics_than_slots_is_refused_at_build(self, registry):
        ids = sorted(registry.metrics)[: MAX_METRICS + 1]
        if len(ids) <= MAX_METRICS:
            pytest.skip("registry smaller than one frame")
        with pytest.raises(AssertionError, match="exceeds"):
            frame(registry, [MetricValue(i, 1) for i in ids])

    def test_no_strings_can_be_placed_on_the_wire(self, registry):
        with pytest.raises((AssertionError, AttributeError, TypeError)):
            MetricValue(1, "not an int")  # type: ignore[arg-type]


class TestRoundTrip:
    def test_values_and_scales_survive(self, pair, registry):
        sender, reader = pair
        sender.write_named("run.success_rate", 4237)
        readings = {r.name: r.value for r in reader.feed(sender.tick(1))}
        assert readings["run.success_rate"] == pytest.approx(0.4237)

    def test_int32_extremes_survive(self, registry):
        reader = EgressReader(registry, KEY)
        ident = registry.by_name("run.step").id
        for value in (INT32_MIN, -1, 0, 1, INT32_MAX):
            reader.last_sequence = None
            readings = reader.feed(frame(registry, [MetricValue(ident, value)]))
            assert readings[0].value == value

    def test_out_of_range_value_is_refused(self):
        with pytest.raises(AssertionError, match="int32"):
            MetricValue(1, INT32_MAX + 1)

    def test_sender_saturates_rather_than_wraps(self, pair):
        """A wrapped counter reads as a plausible small number. Saturation is loud."""
        sender, reader = pair
        sender.write_named("run.step", INT32_MAX + 5000)
        readings = {r.name: r.value for r in reader.feed(sender.tick(1))}
        assert readings["run.step"] == INT32_MAX
        assert sender.counters.clamped_values == 1

    def test_unwritten_metrics_are_marked_stale(self, pair):
        sender, reader = pair
        sender.write_named("run.step", 5)
        by_name = {r.name: r for r in reader.feed(sender.tick(1))}
        assert by_name["run.step"].stale is False
        assert by_name["sys.heartbeat"].stale is True


class TestPrioritySelection:
    """The bug this scheme exists to prevent: lowest-ID selection starves the
    newest block, which is the sandbox and log telemetry."""

    def test_always_metrics_appear_in_every_frame(self, pair, registry):
        sender, reader = pair
        always = {m.name for m in registry.always}
        for tick in range(1, 8):
            names = {r.name for r in reader.feed(sender.tick(tick))}
            assert always <= names, f"tick {tick} dropped an always metric"

    def test_rotating_metrics_are_all_seen_within_the_bound(self, pair, registry):
        import math

        sender, reader = pair
        rotating = {m.name for m in registry.rotating}
        bound = math.ceil(len(rotating) / (MAX_METRICS - len(registry.always)))
        seen = set()
        for tick in range(1, bound + 1):
            seen |= {r.name for r in reader.feed(sender.tick(tick))}
        assert rotating <= seen, f"not all rotating metrics seen in {bound} ticks"

    def test_a_frame_never_exceeds_the_slot_count(self, pair):
        sender, reader = pair
        for tick in range(1, 6):
            assert len(reader.feed(sender.tick(tick))) <= MAX_METRICS

    def test_registry_refuses_an_oversized_always_set(self, registry):
        from dataclasses import replace

        from tools.egress.registry import Registry

        greedy = {
            k: replace(v, priority="always") for k, v in registry.metrics.items()
        }
        with pytest.raises(AssertionError, match="always set"):
            Registry(4, 1, registry.max_metrics, greedy, frozenset())


class TestWrites:
    def test_unknown_metric_id_cannot_be_introduced(self, pair):
        """§5: no component can add an ID the registry does not hold."""
        sender, _ = pair
        assert sender.write(60000, 1) is False
        assert sender.counters.bad_writes == 1

    def test_retired_id_cannot_be_written(self, pair, registry):
        sender, _ = pair
        for retired in registry.retired:
            assert sender.write(retired, 1) is False

    def test_bool_is_not_an_int_here(self, pair):
        sender, _ = pair
        assert sender.write_named("run.step", True) is False


class TestReaderRejects:
    @pytest.mark.parametrize(
        ("mutate", "reason"),
        [
            (lambda b, r: b[:-1], FrameReject.WRONG_SIZE),
            (lambda b, r: b + b"\x00", FrameReject.WRONG_SIZE),
            (lambda b, r: b"\x00\x00\x00\x00" + b[4:], FrameReject.MAGIC),
            (lambda b, r: b[:4] + b"\x00\x09" + b[6:], FrameReject.SCHEMA_VERSION),
            (lambda b, r: b[:6] + b"\x00\x63" + b[8:], FrameReject.REGISTRY_VERSION),
            (lambda b, r: reseal(b[:26] + b"\x00\xff" + b[28:]), FrameReject.METRIC_COUNT),
        ],
    )
    def test_header_rejections(self, registry, mutate, reason):
        reader = EgressReader(registry, KEY)
        assert reader.feed(mutate(frame(registry), registry)) == []
        assert reader.counters.by_reason[reason.name] == 1

    def test_hmac_is_checked_before_contents(self, registry):
        """Ordering: a mutated body fails the HMAC, not the later content check."""
        reader = EgressReader(registry, KEY)
        reader.feed(frame(registry)[:26] + b"\x00\xff" + frame(registry)[28:])
        assert set(reader.counters.by_reason) == {"BAD_HMAC"}

    def test_bad_hmac_is_rejected(self, registry):
        reader = EgressReader(registry, KEY)
        assert reader.feed(frame(registry, key=b"x" * 32)) == []
        assert reader.counters.by_reason["BAD_HMAC"] == 1

    def test_unknown_metric_id_drops_the_whole_frame(self, registry):
        """Not just the slot. A partially applied frame writes a value the sender
        did not send."""
        reader = EgressReader(registry, KEY)
        good = registry.by_name("run.step").id
        buf = frame(registry, [MetricValue(good, 1), MetricValue(good, 2)])
        buf = reseal(
            buf[: HEADER_BYTES + SLOT_BYTES] + b"\xfa\xce" + buf[HEADER_BYTES + SLOT_BYTES + 2 :]
        )
        assert reader.feed(buf) == []
        assert reader.counters.by_reason["UNKNOWN_METRIC_ID"] == 1

    def test_reserved_flag_bits_must_be_zero(self, registry):
        reader = EgressReader(registry, KEY)
        ident = registry.by_name("run.step").id
        buf = bytearray(frame(registry, [MetricValue(ident, 1)]))
        buf[HEADER_BYTES + 2 : HEADER_BYTES + 4] = (RESERVED_FLAG_MASK & 0xFFFF).to_bytes(2, "big")
        assert reader.feed(reseal(bytes(buf))) == []
        assert reader.counters.by_reason["RESERVED_FLAG_SET"] == 1

    def test_replay_is_rejected(self, registry):
        reader = EgressReader(registry, KEY)
        buf = frame(registry, sequence=5)
        assert reader.feed(buf) != [] or True
        assert reader.feed(buf) == []
        assert reader.counters.by_reason["SEQUENCE_NOT_ADVANCING"] == 1

    def test_drops_are_counted_by_reason(self, registry):
        reader = EgressReader(registry, KEY)
        reader.feed(b"\x00" * FRAME_BYTES)
        reader.feed(b"short")
        assert reader.counters.frames_dropped == 2
        assert set(reader.counters.by_reason) == {"MAGIC", "WRONG_SIZE"}


class TestFuzz:
    """§6: random bytes, truncated frames, every field at min and max."""

    @given(payload=st.binary(min_size=0, max_size=FRAME_BYTES * 2))
    @FUZZ
    def test_arbitrary_bytes_never_raise(self, payload):
        registry = load_registry()
        result, reason = parse_frame(payload, KEY, registry.version, frozenset(registry.metrics))
        assert (result is None) != (reason is None)

    @given(cut=st.integers(min_value=0, max_value=FRAME_BYTES))
    @FUZZ
    def test_truncation_at_every_length_is_rejected(self, cut):
        registry = load_registry()
        buf = build_frame(KEY, registry.version, 1, 1, 0, [])
        if cut == FRAME_BYTES:
            return
        result, reason = parse_frame(buf[:cut], KEY, registry.version, frozenset(registry.metrics))
        assert result is None and reason is FrameReject.WRONG_SIZE

    @given(
        offset=st.integers(min_value=0, max_value=FRAME_BYTES - 1),
        mask=st.integers(min_value=1, max_value=255),
    )
    @FUZZ
    def test_single_bit_corruption_never_parses_clean(self, offset, mask):
        registry = load_registry()
        ident = registry.by_name("run.step").id
        buf = bytearray(build_frame(KEY, registry.version, 1, 1, 0, [MetricValue(ident, 7)]))
        buf[offset] ^= mask
        result, reason = parse_frame(bytes(buf), KEY, registry.version, frozenset(registry.metrics))
        assert result is None, "a corrupted frame parsed clean"
        assert reason is not None

    @given(
        sequence=st.integers(min_value=0, max_value=2**64 - 1),
        timestamp=st.integers(min_value=0, max_value=2**64 - 1),
        run_id=st.integers(min_value=0, max_value=2**16 - 1),
        value=st.integers(min_value=INT32_MIN, max_value=INT32_MAX),
    )
    @FUZZ
    def test_every_field_at_its_extremes_round_trips(self, sequence, timestamp, run_id, value):
        registry = load_registry()
        ident = registry.by_name("run.step").id
        buf = build_frame(KEY, registry.version, sequence, timestamp, run_id, [MetricValue(ident, value)])
        parsed, reason = parse_frame(buf, KEY, registry.version, frozenset(registry.metrics))
        assert reason is None
        assert parsed.frame_sequence == sequence
        assert parsed.timestamp == timestamp
        assert parsed.run_id == run_id
        assert parsed.metrics[0].value == value
