"""The two ends of a black link: a shaping sender and a verifying receiver (§4, §5).

`LinkSender` turns messages (an egress frame, a log record, a bundle archive) into
fragments and emits exactly one cell per tick, from its own clock: a queued
fragment if there is one, a cover cell if not. It has no method that receives
anything, so it runs unchanged with a diode on either side of it.

`LinkReceiver` checks each cell's size, magic, link, and replay window, verifies
the tag, and only then reads the fragment inside. Complete messages come out;
everything else is counted and dropped. It has no method that sends anything, and
its counters are read on its own side of the link, never reported to the sender.

Loss is permanent: there is no retransmit, because there is nobody to ask. A sender
that must survive loss sends each message `copies` times, and the receiver fills
gaps in one copy from another and delivers the message once.
"""

from __future__ import annotations

import os
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path

from .cell import (
    FRAG_CAPACITY,
    KEY_BYTES,
    UINT32_MAX,
    UINT64_MAX,
    CellReject,
    Fragment,
    open_cell,
    peek_counter,
    seal_cell,
)

# Counters are reserved on disk in blocks, and the reservation is durable before any
# counter in it is used. A restart skips the rest of the block rather than risk a
# nonce the key has already seen.
COUNTER_BLOCK = 1 << 16
REPLAY_WINDOW = 1024
MAX_FRAGS_PER_MESSAGE = 0xFFFF


def load_link_key(path: Path) -> bytes:
    key = Path(path).read_bytes()
    assert len(key) == KEY_BYTES, f"{path}: a link key is {KEY_BYTES} bytes, got {len(key)}"
    return key


def write_link_key(path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(os.urandom(KEY_BYTES))
    return path


class CounterStore:
    """The sender's durable counter reservation. One file, one integer, fsync'd."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._next = int(self.path.read_text()) if self.path.exists() else 1
        self._reserved_to = self._next  # nothing reserved in this process yet

    def take(self) -> int:
        if self._next >= self._reserved_to:
            self._reserve(self._next + COUNTER_BLOCK)
        n = self._next
        self._next += 1
        return n

    def _reserve(self, upto: int) -> None:
        assert upto <= UINT64_MAX, "counter space exhausted; rotate the link key"
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as fh:
            fh.write(str(upto))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        self._reserved_to = upto


@dataclass
class SenderCounters:
    cells_sent: int = 0
    cover_cells: int = 0
    messages_queued: int = 0
    messages_refused: int = 0

    def as_metrics(self) -> dict[str, int]:
        return {
            "cells_sent": self.cells_sent,
            "cover_cells": self.cover_cells,
            "messages_queued": self.messages_queued,
            "messages_refused": self.messages_refused,
        }


class LinkSender:
    """Encrypting, shaping end. One cell out per `tick()`, always."""

    def __init__(
        self,
        key: bytes,
        link_id: int,
        counter_store: CounterStore,
        *,
        copies: int = 1,
        max_queued_fragments: int = 1 << 16,
    ) -> None:
        assert len(key) == KEY_BYTES
        assert 1 <= copies <= 8
        self._key = key
        self.link_id = link_id
        self._counters = counter_store
        self.copies = copies
        self.max_queued_fragments = max_queued_fragments
        self._queue: deque[Fragment] = deque()
        self._msg_seq = 0
        self.counters = SenderCounters()

    @property
    def queued_fragments(self) -> int:
        return len(self._queue)

    def enqueue(self, message: bytes) -> bool:
        """Queue a message. False if it does not fit; the caller decides what that means.

        Refusal is local: the caller is on this side of the link. Nothing about it
        crosses to the receiver, and nothing about the receiver ever crosses back.
        """
        count = max(1, -(-len(message) // FRAG_CAPACITY))
        if count > MAX_FRAGS_PER_MESSAGE or len(self._queue) + count * self.copies > self.max_queued_fragments:
            self.counters.messages_refused += 1
            return False
        assert self._msg_seq < UINT32_MAX, "message sequence exhausted; rotate the link key"
        self._msg_seq += 1
        frags = [
            Fragment(self._msg_seq, i, count, message[i * FRAG_CAPACITY : (i + 1) * FRAG_CAPACITY])
            for i in range(count)
        ]
        for _ in range(self.copies):
            self._queue.extend(frags)
        self.counters.messages_queued += 1
        return True

    def tick(self) -> bytes:
        fragment = self._queue.popleft() if self._queue else None
        cell = seal_cell(self._key, self.link_id, self._counters.take(), fragment)
        self.counters.cells_sent += 1
        if fragment is None:
            self.counters.cover_cells += 1
        return cell


@dataclass
class ReceiverCounters:
    cells_accepted: int = 0
    cover_cells: int = 0
    messages_delivered: int = 0
    messages_abandoned: int = 0
    duplicate_copies: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    @property
    def cells_dropped(self) -> int:
        return sum(self.by_reason.values())

    def drop(self, reason: CellReject) -> None:
        self.by_reason[reason.name] = self.by_reason.get(reason.name, 0) + 1

    def as_metrics(self) -> dict[str, int]:
        return {
            "cells_accepted": self.cells_accepted,
            "cells_dropped": self.cells_dropped,
            "cover_cells": self.cover_cells,
            "messages_delivered": self.messages_delivered,
            "messages_abandoned": self.messages_abandoned,
        }


@dataclass
class _Partial:
    frag_count: int
    opened_at: int
    size: int = 0
    frags: dict[int, bytes] = field(default_factory=dict)


class ReplayWindow:
    """Sliding bitmap over the counter, as IPsec does: tolerates reorder, refuses repeats."""

    def __init__(self, width: int = REPLAY_WINDOW) -> None:
        self.width = width
        self.highest = 0
        self._seen = 0  # bit i set: counter (highest - i) accepted

    def fresh(self, counter: int) -> bool:
        if counter == 0:
            return False
        if counter > self.highest:
            return True
        offset = self.highest - counter
        return offset < self.width and not (self._seen >> offset) & 1

    def record(self, counter: int) -> None:
        assert self.fresh(counter)
        if counter > self.highest:
            shift = counter - self.highest
            # A jump past the window forgets everything; shifting by the raw jump would
            # let one authenticated cell with a huge counter allocate a huge integer.
            kept = (self._seen << shift) if shift < self.width else 0
            self._seen = (kept | 1) & ((1 << self.width) - 1)
            self.highest = counter
        else:
            self._seen |= 1 << (self.highest - counter)


class LinkReceiver:
    """Verifying end. `feed()` takes any bytes and never raises."""

    def __init__(
        self,
        key: bytes,
        link_id: int,
        *,
        max_open_messages: int = 8,
        max_message_frags: int = MAX_FRAGS_PER_MESSAGE,
        max_open_bytes: int = 256 << 20,
        slack_cells: int = 256,
        remember_delivered: int = 256,
    ) -> None:
        assert len(key) == KEY_BYTES
        self._key = key
        self.link_id = link_id
        self.window = ReplayWindow()
        self.max_open_messages = max_open_messages
        self.max_message_frags = max_message_frags
        self.max_open_bytes = max_open_bytes
        self.slack_cells = slack_cells
        self._open: OrderedDict[int, _Partial] = OrderedDict()
        self._open_bytes = 0
        self._delivered: deque[int] = deque(maxlen=remember_delivered)
        self._clock = 0  # accepted cells; the receiver has no other notion of time
        self.counters = ReceiverCounters()

    def feed(self, buf: bytes) -> list[bytes]:
        counter, reason = peek_counter(buf, self.link_id)
        if reason is None and not self.window.fresh(counter):
            reason = CellReject.REPLAY
        if reason is not None:
            self.counters.drop(reason)
            return []
        counter, fragment, reason = open_cell(buf, self._key, self.link_id)
        if reason is not None:
            self.counters.drop(reason)
            return []

        self.window.record(counter)
        self.counters.cells_accepted += 1
        self._clock += 1
        self._expire()
        if fragment is None:
            self.counters.cover_cells += 1
            return []
        return self._reassemble(fragment)

    def _reassemble(self, f: Fragment) -> list[bytes]:
        if f.msg_seq in self._delivered:
            self.counters.duplicate_copies += 1
            return []
        if f.frag_count > self.max_message_frags:
            self.counters.drop(CellReject.BAD_FRAGMENT)
            return []

        partial = self._open.get(f.msg_seq)
        if partial is None:
            while len(self._open) >= self.max_open_messages:
                self._abandon(next(iter(self._open)))
            partial = self._open[f.msg_seq] = _Partial(f.frag_count, self._clock)
        held = partial.frags.get(f.frag_idx)
        if partial.frag_count != f.frag_count or (held is not None and held != f.payload):
            # Two fragments of one message disagree. Neither can be trusted over the
            # other, so the message goes, whole.
            self._abandon(f.msg_seq)
            self.counters.drop(CellReject.BAD_FRAGMENT)
            return []
        if held is None:
            partial.frags[f.frag_idx] = f.payload
            partial.size += len(f.payload)
            self._open_bytes += len(f.payload)
        # A peer holding the key can still open messages it never finishes. Memory is
        # bounded by what is buffered, not by what a header claims will arrive.
        while self._open_bytes > self.max_open_bytes:
            self._abandon(next(iter(self._open)))
        if f.msg_seq not in self._open or len(partial.frags) < partial.frag_count:
            return []

        del self._open[f.msg_seq]
        self._open_bytes -= partial.size
        self._delivered.append(f.msg_seq)
        self.counters.messages_delivered += 1
        return [b"".join(partial.frags[i] for i in range(partial.frag_count))]

    def _abandon(self, seq: int) -> None:
        self._open_bytes -= self._open.pop(seq).size
        self.counters.messages_abandoned += 1

    def _expire(self) -> None:
        # A message's fragments leave the sender back to back, `copies` times over, so
        # anything open much longer than its own length lost a cell and never will
        # complete. Holding it would only let a lossy link fill the buffer.
        for seq, p in list(self._open.items()):
            if self._clock - p.opened_at > p.frag_count * 8 + self.slack_cells:
                self._abandon(seq)
