"""The run-directory layout note: every entry named, every name in a zone."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from demo_layout import ENTRIES, ZONES, write_layout  # noqa: E402


def test_every_entry_is_in_a_known_zone():
    assert {zone for zone, _ in ENTRIES.values()} <= set(ZONES)


def test_layout_names_only_what_is_present(tmp_path):
    (tmp_path / "outbox").mkdir()
    (tmp_path / "dev-dashboard").mkdir()
    text = write_layout(tmp_path).read_text()
    assert "## OUTSIDE" in text and "## DEV SIDE" in text
    assert "## LOGGING CLUSTER" not in text
    assert "`outbox`" in text and "`sandboxes`" not in text


def test_an_unknown_entry_is_refused(tmp_path):
    (tmp_path / "scratch").mkdir()
    with pytest.raises(AssertionError, match="scratch"):
        write_layout(tmp_path)
