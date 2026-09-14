"""The metric registry must not drift from the spec tables that define it.

Both the sender and the reader compile against schemas/metrics-registry.toml, while
humans read the tables in docs/. If those disagree, the dashboard silently mislabels
values, which is the one failure this channel cannot tolerate: a number with the
wrong name is worse than no number.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from tools.bundle_lint.findings import SCHEMA_DIR

DOCS = SCHEMA_DIR.parent / "docs"
SPEC_DOCS = ("egress-metrics-spec.md", "agent-sandbox-spec.md", "log-diode-spec.md")
ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*`([^`]+)`\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|$"
)
MAX_INT32, MIN_INT32 = 2**31 - 1, -(2**31)


@pytest.fixture(scope="module")
def registry() -> dict:
    with (SCHEMA_DIR / "metrics-registry.toml").open("rb") as fh:
        return tomllib.load(fh)


@pytest.fixture(scope="module")
def documented() -> dict[int, str]:
    rows = {}
    for doc in SPEC_DOCS:
        for line in (DOCS / doc).read_text().splitlines():
            if m := ROW.match(line):
                rows[int(m.group(1))] = m.group(2)
    return rows


def test_docs_yield_rows(documented):
    assert len(documented) > 40, "table parser matched almost nothing; check the docs"


def test_registry_matches_the_documented_tables(registry, documented):
    from_toml = {int(k): v["name"] for k, v in registry["metrics"].items()}
    assert from_toml == documented


def test_ids_and_names_are_unique(registry):
    metrics = registry["metrics"]
    names = [v["name"] for v in metrics.values()]
    assert len(set(names)) == len(names)
    assert len(set(metrics)) == len(metrics)


def test_registry_fits_one_frame(registry):
    """§4: MAX_METRICS per frame. More registered than that is fine; more *live* is
    not, and the sender drops the excess. This asserts the cap is declared."""
    assert registry["max_metrics"] == 64


def test_no_metric_is_a_string(registry):
    """The whole point of the channel. A string-valued metric would defeat it."""
    for mid, metric in registry["metrics"].items():
        assert metric["unit"] in {"count", "seconds", "enum", "scaled"}, mid
        assert isinstance(metric["scale"], int), mid
        assert metric["scale"] >= 1, mid


def test_scaled_metrics_cannot_overflow_int32(registry):
    """A scale so large that the useful range does not fit in int32 is a design bug."""
    for mid, metric in registry["metrics"].items():
        assert metric["scale"] <= MAX_INT32, (mid, metric["name"])


def test_sandbox_block_is_present_and_enumerated(registry):
    """agent-sandbox-spec §7: the escape answer has to survive the numeric channel."""
    by_name = {v["name"]: v for v in registry["metrics"].values()}
    indicator = by_name["sandbox.escape_indicator"]
    assert indicator["unit"] == "enum"
    assert "0 clean" in indicator["values"]
    assert "2 escape" in indicator["values"]
    # Absence of signal is a signal; detector liveness must be its own metric.
    assert "sandbox.detectors_live" in by_name


def test_sandbox_ids_are_in_their_own_block(registry):
    for mid, metric in registry["metrics"].items():
        if metric["name"].startswith("sandbox."):
            assert 80 <= int(mid) <= 89, (mid, metric["name"])


def test_registry_version_bumped_for_the_sandbox_block(registry):
    assert registry["registry_version"] >= 4


def test_retired_ids_are_holes_not_absences(registry):
    """§10: IDs are never reused. A reader on an older registry must not
    reinterpret a value, so retirements are recorded rather than forgotten."""
    retired = {int(k) for k in registry["retired"]}
    assert retired == {53, 60, 61}, "the training-quantity holes"
    assert retired.isdisjoint({int(k) for k in registry["metrics"]})


def test_no_training_quantities_remain(registry):
    """This environment evaluates; it does not train."""
    names = {v["name"] for v in registry["metrics"].values()}
    assert not (names & {"run.loss", "run.lr", "run.grad_norm"})


def test_every_metric_declares_a_priority(registry):
    for mid, metric in registry["metrics"].items():
        assert metric["priority"] in {"always", "rotate"}, mid


def test_always_set_cannot_crowd_out_the_frame(registry):
    """§5: the always set is capped at half of MAX_METRICS, enforced at build."""
    always = [m for m in registry["metrics"].values() if m["priority"] == "always"]
    assert len(always) <= registry["max_metrics"] // 2


def test_the_metrics_whose_absence_is_the_alarm_are_always_sent(registry):
    """The failure this priority scheme exists to prevent: selecting by lowest ID
    starves the highest block, which is the sandbox and log telemetry."""
    by_name = {v["name"]: v for v in registry["metrics"].values()}
    for name in (
        "sandbox.escape_indicator",
        "sandbox.watchdog_state",
        "sandbox.detectors_live",
        "log.sequence_gaps",
        "log.writer_alive",
        "sys.heartbeat",
    ):
        assert by_name[name]["priority"] == "always", name


def test_rotation_interval_is_bounded(registry):
    """Every rotating metric must be seen within a stated number of ticks."""
    import math

    metrics = registry["metrics"].values()
    always = sum(1 for m in metrics if m["priority"] == "always")
    rotate = sum(1 for m in metrics if m["priority"] == "rotate")
    free = registry["max_metrics"] - always
    assert free > 0
    assert math.ceil(rotate / free) <= 4, "rotating metrics go stale for too long"


def test_log_and_broker_blocks_are_registered(registry):
    """Code in tools/brokers emits these; an unregistered metric cannot be sent."""
    by_name = {v["name"]: v for v in registry["metrics"].values()}
    from tools.brokers.action_broker import BrokerCounters

    for name in BrokerCounters().as_metrics():
        assert name in by_name, f"{name} emitted by the broker but not registered"
    assert any(n.startswith("log.") for n in by_name)


def test_every_emitted_counter_is_registered(registry):
    """Any component with an as_metrics() must only emit registered names.

    This is the gap that let the broker ship five unregistered metrics: code and
    registry drifted because nothing checked them against each other.
    """
    from tools.brokers.action_broker import BrokerCounters
    from tools.brokers.env_broker import EnvCounters
    from tools.log_ingest import IngestCounters
    from tools.population import DriverCounters

    by_name = {v["name"] for v in registry["metrics"].values()}
    for counters in (BrokerCounters(), EnvCounters(), IngestCounters(), DriverCounters()):
        for name in counters.as_metrics():
            assert name in by_name, f"{name} emitted by {type(counters).__name__}"


@pytest.mark.parametrize(
    ("prefix", "lo", "hi"),
    [("sandbox.", 80, 89), ("log.", 90, 99), ("broker.", 100, 109), ("driver.", 110, 119)],
)
def test_blocks_stay_in_their_id_ranges(registry, prefix, lo, hi):
    for mid, metric in registry["metrics"].items():
        if metric["name"].startswith(prefix):
            assert lo <= int(mid) <= hi, (mid, metric["name"])
