"""Tests for the diode simulator.

These assert the constraints a real diode imposes, because the point of the
simulator is that code written against it survives the swap to hardware. They do
not assert any security property: the simulator has none, and a test that implied
otherwise would be worse than no test.
"""

from __future__ import annotations

import pytest

from tools.fake_demo_data_diode import DEMO_BANNER, Direction, FakeDemoDataDiode
from tools.fake_demo_data_diode.__main__ import EGRESS_FRAME_BYTES


@pytest.fixture
def sides(tmp_path):
    send, receive = tmp_path / "send", tmp_path / "receive"
    send.mkdir()
    receive.mkdir()
    return send, receive


def diode(sides, **kw):
    return FakeDemoDataDiode(sides[0], sides[1], Direction.INGRESS, **kw)


class TestOneWayness:
    def test_delivers_bytes_unchanged(self, sides):
        send, receive = sides
        (send / "a.bundle").write_bytes(b"\x00\xff" * 512)
        diode(sides).tick()
        assert (receive / "a.bundle").read_bytes() == b"\x00\xff" * 512

    def test_source_is_never_modified_or_removed(self, sides):
        """The receive side cannot reach back and mutate the transmit side."""
        send, _ = sides
        (send / "a.bundle").write_bytes(b"data")
        diode(sides).tick()
        assert (send / "a.bundle").read_bytes() == b"data"

    def test_nothing_flows_backwards(self, sides):
        """Items appearing on the receive side do not travel to the send side."""
        send, receive = sides
        (receive / "reply.json").write_bytes(b"{}")
        diode(sides).tick()
        assert not (send / "reply.json").exists()

    def test_rejects_identical_sides(self, tmp_path):
        with pytest.raises(AssertionError, match="two distinct sides"):
            FakeDemoDataDiode(tmp_path, tmp_path, Direction.INGRESS)

    def test_rejects_nested_sides(self, tmp_path):
        inner = tmp_path / "inner"
        inner.mkdir()
        with pytest.raises(AssertionError, match="must not nest"):
            FakeDemoDataDiode(tmp_path, inner, Direction.INGRESS)


class TestFailureModes:
    """The behaviours a plain `cp` does not have and the receiver must survive."""

    def test_corruption_is_delivered_not_reported(self, sides):
        """A flipped bit arrives looking normal; catching it is the receiver's job."""
        send, receive = sides
        (send / "a.bundle").write_bytes(b"\x00" * 256)
        record = diode(sides, corruption_rate=1.0, seed=0).tick()
        assert record.delivered == ["a.bundle"]
        assert (receive / "a.bundle").read_bytes() != b"\x00" * 256

    def test_drop_leaves_no_trace_on_the_receive_side(self, sides):
        send, receive = sides
        (send / "a.bundle").write_bytes(b"data")
        record = diode(sides, drop_rate=1.0, seed=0).tick()
        assert record.dropped == ["a.bundle"]
        assert list(receive.iterdir()) == []

    def test_rate_cap_defers_rather_than_dropping(self, sides):
        send, receive = sides
        for i in range(5):
            (send / f"{i}.bundle").write_bytes(b"x")
        d = diode(sides, max_items_per_tick=2)
        first = d.tick()
        assert first.delivered == ["0.bundle", "1.bundle"]
        assert first.deferred_rate_cap == ["2.bundle", "3.bundle", "4.bundle"]
        assert first.dropped == [], "over-capacity items are deferred, not lost"
        assert d.tick().delivered == ["2.bundle", "3.bundle"]
        assert d.tick().delivered == ["4.bundle"]
        assert len(list(receive.iterdir())) == 5

    def test_redelivery_is_suppressed_by_name(self, sides):
        send, _ = sides
        (send / "a.bundle").write_bytes(b"data")
        d = diode(sides)
        assert d.tick().delivered == ["a.bundle"]
        assert d.tick().delivered == []

    def test_no_partial_files_are_observable(self, sides):
        """Delivery is atomic: os.replace, never a visible half-written target."""
        send, receive = sides
        (send / "a.bundle").write_bytes(b"x" * 4096)
        diode(sides).tick()
        assert [p.name for p in receive.iterdir()] == ["a.bundle"]

    def test_drain_settles(self, sides):
        send, receive = sides
        for i in range(20):
            (send / f"{i:02d}.bundle").write_bytes(b"x")
        assert len(diode(sides, max_items_per_tick=3).drain().delivered) == 20
        assert len(list(receive.iterdir())) == 20


class TestEgressConstraints:
    def test_oversize_frame_is_not_delivered(self, sides):
        """Egress is fixed-size frames; an oversize item is simply not carried."""
        send, receive = sides
        (send / "frame-0001.bin").write_bytes(b"x" * (EGRESS_FRAME_BYTES + 1))
        d = FakeDemoDataDiode(
            send, receive, Direction.EGRESS, max_item_bytes=EGRESS_FRAME_BYTES
        )
        record = d.tick()
        assert record.dropped_oversize == ["frame-0001.bin"]
        assert list(receive.iterdir()) == []

    def test_frame_size_matches_the_spec(self):
        assert EGRESS_FRAME_BYTES == 572


class TestHonesty:
    def test_banner_says_simulation_and_names_the_fix(self):
        assert "SIMULATION ONLY" in DEMO_BANNER
        assert "no isolation" in DEMO_BANNER
        assert "real one-way hardware" in DEMO_BANNER

    def test_package_docstring_disclaims_security(self):
        import tools.fake_demo_data_diode as pkg

        assert "NOT a diode" in pkg.__doc__
        assert "the airgap is decorative" in pkg.__doc__
