"""The simulator itself.

The design goal is that code written against this behaves identically against real
hardware. Everything a real diode denies you, this denies you too:

  * No return value carries information back to the sender. `transfer` returns what
    the *receive* side observed, and the send side never sees it. The CLI enforces
    this by running the two sides as separate invocations.
  * No acknowledgments, no retransmit requests, no handshake, no error replies.
  * Sequence gaps are permitted and logged; the sender is never told.
  * A malformed or truncated item is dropped at the receive side. The sender does
    not learn that it was dropped (bundle-format-spec §4.2, §8.2).
"""

from __future__ import annotations

import os
import random
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

DEMO_BANNER = (
    "*** fake_demo_data_diode: SIMULATION ONLY. This is a Python process moving "
    "files between two directories on one host. It provides no isolation. Replace "
    "with real one-way hardware for any deployment where isolation matters. ***"
)

# A real diode is rate-limited by its physical link. The simulator caps transfers so
# a demo cannot accidentally depend on unbounded throughput that hardware would not
# give it. Egress in particular is specified as one frame per tick.
DEFAULT_MAX_ITEMS_PER_TICK = 8


class Direction(Enum):
    """Which way this diode points. A diode instance is one-way, always."""

    INGRESS = "ingress"  # outside -> inside: bundles and signed command bundles
    EGRESS = "egress"  # inside -> outside: fixed-size metric frames only


@dataclass
class TransferRecord:
    """What the RECEIVE side observed. The send side never sees this object."""

    delivered: list[str] = field(default_factory=list)
    dropped_corrupt: list[str] = field(default_factory=list)
    dropped_oversize: list[str] = field(default_factory=list)
    # Deferred is not dropped. A real link applies backpressure: items over the
    # tick's capacity wait for the next tick rather than being lost.
    deferred_rate_cap: list[str] = field(default_factory=list)

    @property
    def dropped(self) -> list[str]:
        return sorted(self.dropped_corrupt + self.dropped_oversize)


class FakeDemoDataDiode:
    """One-way file mover with a real diode's constraints and none of its security.

    `send_dir` and `receive_dir` must be distinct. The simulator never writes to
    `send_dir` and never deletes from it: on real hardware the transmit side cannot
    be modified by the receive side, so leaving the source untouched is the honest
    simulation. Deduplication is the receive side's job, by name.
    """

    def __init__(
        self,
        send_dir: Path,
        receive_dir: Path,
        direction: Direction,
        *,
        max_items_per_tick: int = DEFAULT_MAX_ITEMS_PER_TICK,
        max_item_bytes: int | None = None,
        corruption_rate: float = 0.0,
        drop_rate: float = 0.0,
        seed: int | None = None,
    ) -> None:
        send_dir, receive_dir = Path(send_dir).resolve(), Path(receive_dir).resolve()
        assert send_dir != receive_dir, "a diode needs two distinct sides"
        assert not send_dir.is_relative_to(receive_dir), "sides must not nest"
        assert not receive_dir.is_relative_to(send_dir), "sides must not nest"
        assert 0.0 <= corruption_rate <= 1.0, corruption_rate
        assert 0.0 <= drop_rate <= 1.0, drop_rate

        self.send_dir = send_dir
        self.receive_dir = receive_dir
        self.direction = direction
        self.max_items_per_tick = max_items_per_tick
        self.max_item_bytes = max_item_bytes
        self.corruption_rate = corruption_rate
        self.drop_rate = drop_rate
        self._rng = random.Random(seed)

    def tick(self) -> TransferRecord:
        """Move one tick's worth of items from send side to receive side.

        Returns what the receive side observed. On real hardware nobody on the send
        side could obtain this; the CLI keeps that honest by running the sides as
        separate processes.
        """
        self.receive_dir.mkdir(parents=True, exist_ok=True)
        record = TransferRecord()

        # Dedup before applying the rate cap, or already-delivered items consume
        # this tick's capacity and the queue never drains.
        pending = [
            p
            for p in sorted(self.send_dir.iterdir())
            if p.is_file() and not (self.receive_dir / p.name).exists()
        ]
        for item in pending[self.max_items_per_tick :]:
            record.deferred_rate_cap.append(item.name)

        for item in pending[: self.max_items_per_tick]:
            size = item.stat().st_size
            if self.max_item_bytes is not None and size > self.max_item_bytes:
                # A real link has an MTU and a frame size. Oversize is not
                # negotiable down; it is simply not delivered, and nobody is told.
                record.dropped_oversize.append(item.name)
                continue

            if self._rng.random() < self.drop_rate:
                record.dropped_corrupt.append(item.name)
                continue

            self._deliver(item, corrupt=self._rng.random() < self.corruption_rate)
            record.delivered.append(item.name)

        return record

    def _deliver(self, item: Path, *, corrupt: bool) -> None:
        """Write atomically: a receiver must never observe a half-written item."""
        target = self.receive_dir / item.name
        staging = self.receive_dir / f".{item.name}.partial"
        shutil.copyfile(item, staging)

        if corrupt:
            # Bit rot on the link. The receiver catches this via the manifest hash
            # (ingress, §4.3) or the HMAC (egress, §4), and fails closed. That path
            # is the reason this knob exists.
            data = bytearray(staging.read_bytes())
            if data:
                index = self._rng.randrange(len(data))
                data[index] ^= 1 << self._rng.randrange(8)
                staging.write_bytes(data)

        os.replace(staging, target)

    def drain(self, max_ticks: int = 1000) -> TransferRecord:
        """Tick until nothing new moves. For demos, not for deployments."""
        total = TransferRecord()
        for _ in range(max_ticks):
            record = self.tick()
            if not (record.delivered or record.dropped):
                return total
            total.delivered += record.delivered
            total.dropped_corrupt += record.dropped_corrupt
            total.dropped_oversize += record.dropped_oversize
            total.deferred_rate_cap = record.deferred_rate_cap
        raise AssertionError(f"drain did not settle in {max_ticks} ticks")
