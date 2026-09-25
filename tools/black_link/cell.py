"""The black-side cell. Hand-written, fixed size, authenticated before parsed (§4).

    offset  size   field                        protection
    0       4      magic       0x424C4B31       authenticated (AAD)
    4       4      link_id     uint32           authenticated (AAD)
    8       8      counter     uint64           authenticated (AAD); also the nonce
    16      1248   ciphertext  XChaCha20        encrypted + authenticated
    1264    16     tag         Poly1305

    inner plaintext (1248 bytes, only ever read after the tag verifies):
    0       1      kind        0 cover, 1 data
    1       1      reserved    zero
    2       4      msg_seq     uint32, per link
    6       2      frag_idx    uint16
    8       2      frag_count  uint16, >= 1
    10      2      length      uint16, <= FRAG_CAPACITY
    12      1236   payload     then zero padding to the end

Every cell is CELL_BYTES on the wire whatever it carries, and a cover cell is
encrypted exactly like a data cell. An observer on the black network sees a cell
per tick and learns nothing from its size, its timing, or whether it was cover.

There is no handshake. The key is a 32-byte symmetric key per link per direction,
provisioned physically, and the nonce is the link id and counter under a key used
in one direction only, so a nonce never repeats. That is what lets the sending end
sit behind a diode: it never needs to hear anything from the far side.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_ABYTES as TAG_BYTES,
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
    crypto_aead_xchacha20poly1305_ietf_KEYBYTES as KEY_BYTES,
    crypto_aead_xchacha20poly1305_ietf_NPUBBYTES as NONCE_BYTES,
)
from nacl.exceptions import CryptoError

MAGIC = 0x424C4B31  # "BLK1"
# 1280 is the IPv6 minimum MTU: one cell per datagram on any path, no fragmentation
# on the black network, and so no reassembly code in front of the tag check.
CELL_BYTES = 1280
HEADER_BYTES = 16
INNER_BYTES = CELL_BYTES - HEADER_BYTES - TAG_BYTES  # 1248
INNER_HEADER_BYTES = 12
FRAG_CAPACITY = INNER_BYTES - INNER_HEADER_BYTES  # 1236

KIND_COVER = 0
KIND_DATA = 1

UINT32_MAX = 2**32 - 1
UINT64_MAX = 2**64 - 1


class CellReject(IntEnum):
    WRONG_SIZE = 1
    MAGIC = 2
    WRONG_LINK = 3
    REPLAY = 4
    BAD_TAG = 5
    # Everything below is only reachable with the peer's key. It is a bug or a
    # compromised peer, never a stranger on the black network.
    UNKNOWN_KIND = 6
    RESERVED_SET = 7
    BAD_FRAGMENT = 8
    NONZERO_PADDING = 9


@dataclass(frozen=True)
class Fragment:
    msg_seq: int
    frag_idx: int
    frag_count: int
    payload: bytes


def _nonce(link_id: int, counter: int) -> bytes:
    # The link id is in the nonce as well as the AAD, so a key mistakenly shared
    # between two links still never sees the same nonce twice.
    return link_id.to_bytes(4, "big") + bytes(NONCE_BYTES - 12) + counter.to_bytes(8, "big")


def _header(link_id: int, counter: int) -> bytes:
    return MAGIC.to_bytes(4, "big") + link_id.to_bytes(4, "big") + counter.to_bytes(8, "big")


def seal_cell(key: bytes, link_id: int, counter: int, fragment: Fragment | None) -> bytes:
    """One cell. `fragment=None` is a cover cell."""
    assert len(key) == KEY_BYTES
    assert 0 <= link_id <= UINT32_MAX
    assert 0 < counter <= UINT64_MAX, "counter 0 is never sent; it is the receiver's floor"
    inner = bytearray(INNER_BYTES)
    if fragment is not None:
        f = fragment
        assert 0 <= f.msg_seq <= UINT32_MAX
        assert 1 <= f.frag_count <= 0xFFFF and 0 <= f.frag_idx < f.frag_count
        assert len(f.payload) <= FRAG_CAPACITY
        inner[0] = KIND_DATA
        inner[2:6] = f.msg_seq.to_bytes(4, "big")
        inner[6:8] = f.frag_idx.to_bytes(2, "big")
        inner[8:10] = f.frag_count.to_bytes(2, "big")
        inner[10:12] = len(f.payload).to_bytes(2, "big")
        inner[12 : 12 + len(f.payload)] = f.payload
    header = _header(link_id, counter)
    nonce = _nonce(link_id, counter)
    sealed = crypto_aead_xchacha20poly1305_ietf_encrypt(bytes(inner), header, nonce, key)
    cell = header + sealed
    assert len(cell) == CELL_BYTES
    return cell


def peek_counter(buf: bytes, link_id: int) -> tuple[int | None, CellReject | None]:
    """The unauthenticated pre-checks: size, magic, link. Cheap, and all a stranger reaches."""
    if len(buf) != CELL_BYTES:
        return None, CellReject.WRONG_SIZE
    if int.from_bytes(buf[0:4], "big") != MAGIC:
        return None, CellReject.MAGIC
    if int.from_bytes(buf[4:8], "big") != link_id:
        return None, CellReject.WRONG_LINK
    return int.from_bytes(buf[8:16], "big"), None


def open_cell(
    buf: bytes, key: bytes, link_id: int
) -> tuple[int | None, Fragment | None, CellReject | None]:
    """Open one cell. Returns `(counter, fragment_or_None_for_cover, None)` or a reason.

    Never raises. The order is the point: size, magic, link, then the tag, and only
    then any field inside. Replay is the caller's check because it needs the window,
    and the caller must make it before recording the counter.
    """
    counter, reason = peek_counter(buf, link_id)
    if reason is not None:
        return None, None, reason
    try:
        inner = crypto_aead_xchacha20poly1305_ietf_decrypt(
            bytes(buf[HEADER_BYTES:]), bytes(buf[:HEADER_BYTES]), _nonce(link_id, counter), key
        )
    except CryptoError:
        return None, None, CellReject.BAD_TAG

    kind = inner[0]
    if inner[1] != 0:
        return None, None, CellReject.RESERVED_SET
    if kind == KIND_COVER:
        if any(inner):
            return None, None, CellReject.NONZERO_PADDING
        return counter, None, None
    if kind != KIND_DATA:
        return None, None, CellReject.UNKNOWN_KIND

    msg_seq = int.from_bytes(inner[2:6], "big")
    frag_idx = int.from_bytes(inner[6:8], "big")
    frag_count = int.from_bytes(inner[8:10], "big")
    length = int.from_bytes(inner[10:12], "big")
    if frag_count == 0 or frag_idx >= frag_count or length > FRAG_CAPACITY:
        return None, None, CellReject.BAD_FRAGMENT
    # Every fragment but the last is full, so a message's length is fixed by its
    # fragment count and the last fragment alone. Nothing to trust about a total.
    if frag_idx < frag_count - 1 and length != FRAG_CAPACITY:
        return None, None, CellReject.BAD_FRAGMENT
    if frag_idx == frag_count - 1 and length == 0 and frag_count > 1:
        return None, None, CellReject.BAD_FRAGMENT
    end = INNER_HEADER_BYTES + length
    if any(inner[end:]):
        return None, None, CellReject.NONZERO_PADDING
    return counter, Fragment(msg_seq, frag_idx, frag_count, bytes(inner[INNER_HEADER_BYTES:end])), None
