"""black-link: the reference one-way link encryptor (remote-link-spec §4, §5).

Carries an existing one-way link -- ingress, egress, or log -- across a network
nobody here controls, between two sites that each keep their own diodes. It adds
confidentiality, integrity, and traffic-flow security to the black segment. It
adds no path: the sender cannot receive and the receiver cannot send, so the
link can be diode-enforced at both ends and usually should be.

What it replaces is a hardware inline encryptor (HAIPE-class, or a CSfC stack),
the same way `tools/fake_demo_data_diode` stands in for a diode. The protocol is
the part to keep: handshake-less AEAD under a physically provisioned key, fixed
cells at a fixed rate, authenticate before parse, fail closed. The software is
reference-grade and does not claim the assurance of the hardware it describes.

Nothing here runs inside the airgap, so nothing here reports on the egress
channel. Each end's counters are read at that end.
"""

from .cell import CELL_BYTES, FRAG_CAPACITY, KEY_BYTES, CellReject, Fragment, open_cell, seal_cell
from .link import (
    CounterStore,
    LinkReceiver,
    LinkSender,
    ReceiverCounters,
    ReplayWindow,
    SenderCounters,
    load_link_key,
    write_link_key,
)

__all__ = [
    "CELL_BYTES",
    "FRAG_CAPACITY",
    "KEY_BYTES",
    "CellReject",
    "CounterStore",
    "Fragment",
    "LinkReceiver",
    "LinkSender",
    "ReceiverCounters",
    "ReplayWindow",
    "SenderCounters",
    "load_link_key",
    "open_cell",
    "seal_cell",
    "write_link_key",
]
