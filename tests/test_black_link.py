"""black-link: the one-way link encryptor that carries a diode'd link between sites.

The properties under test are the ones remote-link-spec §4-§5 rests on: every cell
is the same size whatever it carries, a stranger's bytes never get past the tag,
nothing is replayed, loss is survivable only by sending copies, and the receiver's
memory is bounded by what it has buffered rather than by what a header claims.

The fuzz harness remote-link-spec §5.4 asks for is at the bottom.
"""

from __future__ import annotations

import os

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tools.black_link import (
    CELL_BYTES,
    FRAG_CAPACITY,
    CellReject,
    CounterStore,
    Fragment,
    LinkReceiver,
    LinkSender,
    ReplayWindow,
    open_cell,
    seal_cell,
)
from tools.black_link.__main__ import main as cli
from tools.black_link.cell import HEADER_BYTES, KEY_BYTES

KEY = b"K" * KEY_BYTES
OTHER_KEY = b"k" * KEY_BYTES
LINK = 7
FUZZ = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


@pytest.fixture
def sender(tmp_path) -> LinkSender:
    return LinkSender(KEY, LINK, CounterStore(tmp_path / "counter"))


def pump(sender: LinkSender, receiver: LinkReceiver, ticks: int) -> list[bytes]:
    out = []
    for _ in range(ticks):
        out += receiver.feed(sender.tick())
    return out


# --- cells -----------------------------------------------------------------------


def test_cover_and_data_cells_are_the_same_size():
    cover = seal_cell(KEY, LINK, 1, None)
    data = seal_cell(KEY, LINK, 2, Fragment(1, 0, 1, b"x" * FRAG_CAPACITY))
    empty = seal_cell(KEY, LINK, 3, Fragment(2, 0, 1, b""))
    assert len(cover) == len(data) == len(empty) == CELL_BYTES


def test_round_trip():
    f = Fragment(9, 2, 3, b"payload")  # the last fragment may be short
    assert open_cell(seal_cell(KEY, LINK, 5, f), KEY, LINK) == (5, f, None)
    assert open_cell(seal_cell(KEY, LINK, 6, None), KEY, LINK) == (6, None, None)


def test_header_is_authenticated():
    cell = bytearray(seal_cell(KEY, LINK, 5, None))
    cell[15] ^= 1  # counter: the nonce and the AAD both change
    assert open_cell(bytes(cell), KEY, LINK)[2] is CellReject.BAD_TAG


def test_wrong_key_and_wrong_link_are_refused():
    cell = seal_cell(KEY, LINK, 5, None)
    assert open_cell(cell, OTHER_KEY, LINK)[2] is CellReject.BAD_TAG
    assert open_cell(cell, KEY, LINK + 1)[2] is CellReject.WRONG_LINK


def test_same_key_on_two_links_does_not_reuse_a_nonce():
    """A provisioning mistake, not a supported configuration, but not a catastrophe either."""
    a = seal_cell(KEY, 1, 5, None)
    b = seal_cell(KEY, 2, 5, None)
    assert a[HEADER_BYTES:] != b[HEADER_BYTES:]


def test_cover_cells_do_not_look_like_zeros():
    """A cover cell's plaintext is all zeros; its ciphertext must not be, or cover is visible."""
    a, b = seal_cell(KEY, LINK, 1, None), seal_cell(KEY, LINK, 2, None)
    assert a[HEADER_BYTES:] != b[HEADER_BYTES:]
    assert a[HEADER_BYTES:].count(0) < 64


@pytest.mark.parametrize(
    "frag",
    [
        Fragment(1, 0, 2, b"short"),  # a non-final fragment must be full
        Fragment(1, 1, 2, b""),  # a final fragment of a multi-fragment message is non-empty
    ],
)
def test_fragment_shape_rules(frag):
    assert open_cell(seal_cell(KEY, LINK, 1, frag), KEY, LINK)[2] is CellReject.BAD_FRAGMENT


# --- sender ----------------------------------------------------------------------


def test_sender_emits_exactly_one_cell_per_tick_whether_or_not_it_has_data(sender):
    cells = [sender.tick() for _ in range(3)]
    sender.enqueue(b"frame")
    cells += [sender.tick() for _ in range(3)]
    assert all(len(c) == CELL_BYTES for c in cells)
    assert sender.counters.cells_sent == 6 and sender.counters.cover_cells == 5


def test_sender_has_no_receive_path():
    assert not any(hasattr(LinkSender, name) for name in ("feed", "receive", "recv", "ack"))
    assert not any(hasattr(LinkReceiver, name) for name in ("tick", "send", "enqueue", "ack"))


def test_counter_survives_restart_without_reuse(tmp_path):
    store = tmp_path / "counter"
    first = LinkSender(KEY, LINK, CounterStore(store))
    used = {int.from_bytes(first.tick()[8:16], "big") for _ in range(5)}
    second = LinkSender(KEY, LINK, CounterStore(store))  # a crash and restart
    after = {int.from_bytes(second.tick()[8:16], "big") for _ in range(5)}
    assert not used & after
    assert min(after) > max(used)


def test_queue_bound_refuses_locally(tmp_path):
    s = LinkSender(KEY, LINK, CounterStore(tmp_path / "c"), max_queued_fragments=2)
    assert s.enqueue(b"a" * FRAG_CAPACITY * 2)
    assert not s.enqueue(b"b")
    assert s.counters.messages_refused == 1


# --- receiver --------------------------------------------------------------------


def test_messages_of_every_size_arrive_whole(sender):
    rx = LinkReceiver(KEY, LINK)
    messages = [b"", b"x", os.urandom(FRAG_CAPACITY), os.urandom(FRAG_CAPACITY + 1), os.urandom(10_000)]
    for m in messages:
        assert sender.enqueue(m)
    assert pump(sender, rx, 40) == messages
    assert rx.counters.cover_cells > 0
    assert rx.counters.cells_dropped == 0


def test_egress_frame_fits_one_cell():
    from tools.egress import FRAME_BYTES

    assert FRAME_BYTES <= FRAG_CAPACITY


def test_replay_is_refused_before_the_tag_is_checked(sender):
    rx = LinkReceiver(KEY, LINK)
    sender.enqueue(b"once")
    cell = sender.tick()
    assert rx.feed(cell) == [b"once"]
    assert rx.feed(cell) == []
    assert rx.counters.by_reason == {"REPLAY": 1}


def test_reorder_within_the_window_is_accepted(sender):
    rx = LinkReceiver(KEY, LINK)
    sender.enqueue(os.urandom(FRAG_CAPACITY * 3))
    cells = [sender.tick() for _ in range(3)]
    out = []
    for c in reversed(cells):
        out += rx.feed(c)
    assert len(out) == 1 and rx.counters.cells_dropped == 0


def test_replay_window_edges():
    w = ReplayWindow(width=4)
    assert not w.fresh(0)
    for n in (10, 8):
        w.record(n)
    assert not w.fresh(10) and not w.fresh(8)
    assert w.fresh(9) and w.fresh(7)
    assert not w.fresh(6)  # outside the window: too old to tell, so refused


def test_replay_window_survives_a_huge_jump():
    w = ReplayWindow()
    w.record(1)
    w.record(2**64 - 1)
    assert w._seen == 1 and not w.fresh(2**64 - 1) and not w.fresh(1)


def test_lost_cell_loses_the_message_without_copies(sender):
    rx = LinkReceiver(KEY, LINK, slack_cells=4)
    sender.enqueue(os.urandom(FRAG_CAPACITY * 3))
    cells = [sender.tick() for _ in range(3)]
    for c in (cells[0], cells[2]):
        rx.feed(c)
    for _ in range(64):
        rx.feed(sender.tick())
    assert rx.counters.messages_delivered == 0
    assert rx.counters.messages_abandoned == 1


def test_copies_fill_gaps_and_deliver_once(tmp_path):
    s = LinkSender(KEY, LINK, CounterStore(tmp_path / "c"), copies=2)
    rx = LinkReceiver(KEY, LINK)
    message = os.urandom(FRAG_CAPACITY * 3)
    s.enqueue(message)
    cells = [s.tick() for _ in range(6)]
    got = []
    for i, c in enumerate(cells):
        if i in (1, 3):  # lose a different fragment from each copy
            continue
        got += rx.feed(c)
    assert got == [message]
    assert rx.counters.duplicate_copies >= 1


def test_open_messages_are_bounded(tmp_path):
    rx = LinkReceiver(KEY, LINK, max_open_messages=2)
    for seq in range(1, 6):
        rx.feed(seal_cell(KEY, LINK, seq, Fragment(seq, 0, 2, b"x" * FRAG_CAPACITY)))
    assert len(rx._open) == 2
    assert rx.counters.messages_abandoned == 3


def test_open_bytes_are_bounded_by_what_is_buffered():
    rx = LinkReceiver(KEY, LINK, max_open_bytes=FRAG_CAPACITY * 3)
    for i in range(10):
        rx.feed(seal_cell(KEY, LINK, i + 1, Fragment(1, i, 60_000, b"x" * FRAG_CAPACITY)))
    assert rx._open_bytes <= FRAG_CAPACITY * 3


def test_disagreeing_fragments_drop_the_whole_message():
    rx = LinkReceiver(KEY, LINK)
    rx.feed(seal_cell(KEY, LINK, 1, Fragment(1, 0, 2, b"a" * FRAG_CAPACITY)))
    rx.feed(seal_cell(KEY, LINK, 2, Fragment(1, 0, 2, b"b" * FRAG_CAPACITY)))
    assert rx.feed(seal_cell(KEY, LINK, 3, Fragment(1, 1, 2, b"end"))) == []
    assert rx.counters.by_reason["BAD_FRAGMENT"] == 1


def test_cli_round_trip(tmp_path):
    key = tmp_path / "link.key"
    assert cli(["keygen", "--out", str(key)]) == 0
    spool, cells, deliver = tmp_path / "spool", tmp_path / "cells", tmp_path / "deliver"
    spool.mkdir()
    (spool / "a.bin").write_bytes(b"alpha")
    (spool / "b.bin").write_bytes(os.urandom(5000))
    expected = [(spool / n).read_bytes() for n in ("a.bin", "b.bin")]
    common = ["--key", str(key), "--link-id", "3"]
    assert cli(["send", *common, "--state", str(tmp_path / "ctr"), "--spool", str(spool),
                "--cells", str(cells), "--ticks", "10"]) == 0
    assert not list(spool.iterdir())
    assert len(list(cells.iterdir())) == 10
    assert cli(["receive", *common, "--cells", str(cells), "--deliver", str(deliver)]) == 0
    assert [p.read_bytes() for p in sorted(deliver.iterdir())] == expected


def test_keygen_refuses_to_overwrite(tmp_path):
    key = tmp_path / "link.key"
    cli(["keygen", "--out", str(key)])
    with pytest.raises(FileExistsError):
        cli(["keygen", "--out", str(key)])


# --- fuzz: remote-link-spec §5.4 ---------------------------------------------------


@FUZZ
@given(st.binary(max_size=CELL_BYTES * 2))
def test_fuzz_random_bytes_never_raise_or_deliver(buf):
    rx = LinkReceiver(KEY, LINK)
    assert rx.feed(buf) == []
    assert rx.counters.cells_accepted == 0


@FUZZ
@given(st.integers(0, CELL_BYTES - 1), st.integers(1, 255))
def test_fuzz_every_single_byte_flip_is_refused(at, mask):
    cell = bytearray(seal_cell(KEY, LINK, 1, Fragment(1, 0, 1, b"frame")))
    cell[at] ^= mask
    rx = LinkReceiver(KEY, LINK)
    assert rx.feed(bytes(cell)) == []
    assert rx.counters.cells_accepted == 0


@FUZZ
@given(st.integers(0, CELL_BYTES))
def test_fuzz_truncation(n):
    cell = seal_cell(KEY, LINK, 1, None)
    rx = LinkReceiver(KEY, LINK)
    rx.feed(cell[:n])
    assert rx.counters.cells_accepted == (1 if n == CELL_BYTES else 0)


@FUZZ
@given(
    st.integers(0, 2**32 - 1),
    st.integers(0, 0xFFFF),
    st.integers(0, 0xFFFF),
    st.binary(max_size=FRAG_CAPACITY),
)
def test_fuzz_authenticated_but_hostile_fragments_never_raise(msg_seq, idx, count, payload):
    """What a compromised peer holding the key can send. Bounded, never a crash."""
    if not (1 <= count and idx < count):
        return  # seal_cell refuses to build these; open_cell's own checks are tested above
    rx = LinkReceiver(KEY, LINK, max_open_bytes=FRAG_CAPACITY * 4)
    try:
        cell = seal_cell(KEY, LINK, 1, Fragment(msg_seq, idx, count, payload))
    except AssertionError:
        return
    out = rx.feed(cell)
    assert rx._open_bytes <= FRAG_CAPACITY * 4
    assert all(isinstance(m, bytes) for m in out)


@FUZZ
@given(st.binary(min_size=0, max_size=1248 - 12))
def test_fuzz_raw_inner_plaintext(inner_tail):
    """Encrypt arbitrary inner plaintext under the right key: the post-tag parser must hold."""
    from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_encrypt

    from tools.black_link.cell import INNER_BYTES, _header, _nonce

    for kind in (0, 1, 2):
        inner = bytes([kind]) + inner_tail[:INNER_BYTES - 1]
        inner = inner.ljust(INNER_BYTES, b"\0")
        header = _header(LINK, 1)
        cell = header + crypto_aead_xchacha20poly1305_ietf_encrypt(inner, header, _nonce(LINK, 1), KEY)
        rx = LinkReceiver(KEY, LINK)
        out = rx.feed(cell)
        assert rx.counters.cells_accepted + rx.counters.cells_dropped == 1
        assert all(isinstance(m, bytes) for m in out)
