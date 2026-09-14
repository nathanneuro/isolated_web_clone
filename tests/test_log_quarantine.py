"""The quarantine tier: does anything reach the readable side without passing?"""

from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tools.log_ingest import NullScanner, QuarantineTier, Severity, Stream, build_record

KEY = b"k" * 32
SOURCES = frozenset({7, 9})


class RefusingScanner:
    name = "refuses-everything"

    def __call__(self, payload: bytes) -> bool:
        return False


class SignatureScanner:
    name = "test-signature"

    def __call__(self, payload: bytes) -> bool:
        return b"EICAR-TEST" not in payload


def tier(tmp_path, scanner=None, **kw):
    return QuarantineTier(tmp_path, KEY, SOURCES, scanner or NullScanner(), **kw)


def rec(payload=b"hello", *, source=7, sequence=1, stream=Stream.SYSTEM, key=KEY):
    return build_record(key, source, sequence, 1700000000, stream, Severity.INFO, payload)


class TestPromotion:
    def test_valid_records_are_promoted_with_both_copies(self, tmp_path):
        q = tier(tmp_path)
        q.ingest(rec(b"clean event"))
        promoted = tmp_path / "promoted" / "system" / "7"
        assert (promoted / "1.rec").read_bytes() == b"clean event"
        assert (promoted / "1.txt").read_text() == "clean event"

    def test_original_is_kept_byte_exact(self, tmp_path):
        """The .rec is evidence. Sanitisation only ever produces the .txt."""
        q = tier(tmp_path)
        payload = b"before\x1b[2K\x00after\xff"
        q.ingest(rec(payload))
        promoted = tmp_path / "promoted" / "system" / "7"
        assert (promoted / "1.rec").read_bytes() == payload
        assert "\x1b" not in (promoted / "1.txt").read_text()

    def test_index_holds_header_fields_only(self, tmp_path):
        """No payload-derived value in the index, or it becomes a query surface."""
        q = tier(tmp_path)
        q.ingest(rec(b"a secret string that must not be indexed"))
        entry = json.loads((tmp_path / "index.jsonl").read_text().strip())
        assert set(entry) == {
            "source_id", "sequence", "timestamp", "stream", "severity",
            "payload_bytes", "stripped",
        }
        assert "secret" not in json.dumps(entry)

    def test_streams_are_separated_on_disk(self, tmp_path):
        q = tier(tmp_path)
        q.ingest(rec(b"a", sequence=1, stream=Stream.WATCHDOG))
        q.ingest(rec(b"b", sequence=2, stream=Stream.TRAJECTORY))
        assert (tmp_path / "promoted" / "watchdog" / "7" / "1.rec").exists()
        assert (tmp_path / "promoted" / "trajectory" / "7" / "2.rec").exists()


class TestPathsNeverComeFromPayload:
    @pytest.mark.parametrize(
        "payload",
        [
            b"../../etc/cron.d/evil",
            b"/etc/passwd",
            b"..\\..\\windows\\system32",
            b"\x00/dev/null",
            b"a" * 200 + b"/../../../x",
        ],
    )
    def test_hostile_payload_cannot_choose_its_location(self, tmp_path, payload):
        q = tier(tmp_path)
        report = q.ingest(rec(payload))
        assert report.promoted == ["system/7/1"]
        written = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}
        assert written == {
            "promoted/system/7/1.rec", "promoted/system/7/1.txt", "index.jsonl",
        }


class TestFailClosed:
    def test_malware_finding_is_not_promoted(self, tmp_path):
        q = tier(tmp_path, SignatureScanner())
        report = q.ingest(rec(b"payload with EICAR-TEST inside"))
        assert report.promoted == []
        assert q.counters.malware_findings == 1
        assert not (tmp_path / "promoted" / "system").exists()

    def test_malware_finding_is_retained_as_evidence(self, tmp_path):
        """Discarding it would destroy the evidence of whatever produced it."""
        q = tier(tmp_path, SignatureScanner())
        q.ingest(rec(b"payload with EICAR-TEST inside"))
        held = tmp_path / "quarantine" / "7"
        assert (held / "1.rec").read_bytes() == b"payload with EICAR-TEST inside"
        assert json.loads((held / "1.why.json").read_text())["scanner"] == "test-signature"

    def test_bad_hmac_is_quarantined_not_promoted(self, tmp_path):
        q = tier(tmp_path)
        report = q.ingest(rec(b"forged", key=b"x" * 32))
        assert report.promoted == []
        assert q.counters.hmac_rejects >= 1
        assert q.counters.records_quarantined >= 1

    def test_unknown_source_is_not_promoted(self, tmp_path):
        q = tier(tmp_path)
        report = q.ingest(rec(b"x", source=404))
        assert report.promoted == []
        assert q.counters.records_ingested == 0
        assert q.counters.framing_rejects >= 1

    def test_a_scanner_must_be_named_explicitly(self, tmp_path):
        """No silent default: running without scanning is a decision, not an omission."""
        with pytest.raises(AssertionError, match="NullScanner"):
            QuarantineTier(tmp_path, KEY, SOURCES, None)

    def test_null_scanner_says_what_it_does(self):
        assert "scans-nothing" in NullScanner().name


class TestStreamHandling:
    def test_partial_trailing_record_is_left_for_the_next_delivery(self, tmp_path):
        """A diode has no flow control; a record can straddle a delivery."""
        q = tier(tmp_path)
        whole = rec(b"first") + rec(b"second", sequence=2)
        cut = len(rec(b"first")) + 10
        report = q.ingest(whole[:cut])
        assert report.promoted == ["system/7/1"]
        assert report.undecodable_tail == 10
        second = q.ingest(whole[report.bytes_consumed :])
        assert second.promoted == ["system/7/2"]

    def test_sequence_gaps_are_counted(self, tmp_path):
        """A gap is how truncation looks from outside (§7)."""
        q = tier(tmp_path)
        q.ingest(rec(b"a", sequence=1))
        q.ingest(rec(b"b", sequence=9))
        assert q.counters.sequence_gaps == 7

    def test_replayed_sequence_is_not_promoted_twice(self, tmp_path):
        q = tier(tmp_path)
        q.ingest(rec(b"a", sequence=5))
        report = q.ingest(rec(b"a", sequence=5))
        assert report.promoted == []

    def test_counter_names_match_the_registry(self, tmp_path):
        import tomllib

        from tools.bundle_lint.findings import SCHEMA_DIR

        with (SCHEMA_DIR / "metrics-registry.toml").open("rb") as fh:
            known = {v["name"] for v in tomllib.load(fh)["metrics"].values()}
        for name in tier(tmp_path).counters.as_metrics():
            assert name in known, f"{name} emitted but not registered"


class TestFuzz:
    @given(payload=st.binary(min_size=0, max_size=1024))
    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_arbitrary_stream_never_raises_and_never_escapes(self, tmp_path, payload):
        q = tier(tmp_path)
        q.ingest(payload)
        for path in tmp_path.rglob("*"):
            assert tmp_path in path.resolve().parents or path.resolve() == tmp_path
