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
SPEC_DOCS = ("egress-metrics-spec.md", "agent-sandbox-spec.md")
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
    assert registry["registry_version"] >= 2
