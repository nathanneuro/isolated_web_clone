"""fake_demo_data_diode -- a SIMULATION of a hardware data diode. NOT a diode.

=============================================================================
READ THIS BEFORE DEPLOYING ANYTHING
=============================================================================

A real data diode is a piece of hardware with no return path in it: a fiber
transmitter facing a fiber receiver with no transmitter on the far end, or an
equivalent. Its security property comes from physics. Bytes cannot flow backwards
because there is no medium for them to flow backwards through.

This module provides none of that. It is an ordinary Python process moving files
between two directories on one host. It can read both sides. The kernel it runs on
can read both sides. Anything that compromises the host has both sides. If you put
this in a deployment that is supposed to be airgapped, the airgap is decorative.

What it IS for:

  * Running the pipeline end to end on one laptop, so the demo in the README works
    and so the receiver, worker, go-live, and egress reader can be developed and
    tested without buying hardware.
  * Demonstrating the PROTOCOL a real diode imposes -- no acknowledgments, no
    back-channel, no retransmit requests, fail-closed on malformed input -- so the
    components on either side are written to survive those constraints from day one.
  * Exercising the failure modes (dropped transfers, truncated files, replayed
    sequence numbers) that a real one-way link produces and a normal file copy
    does not.

What it is NOT for:

  * Any deployment where the isolation is load-bearing. Replace it with real
    hardware. The components on either side need no changes when you do: they
    already assume there is no way to ask the other side anything.

The name is deliberately unmissable for the same reason.
"""

from .diode import DEMO_BANNER, Direction, FakeDemoDataDiode, TransferRecord

__all__ = ["DEMO_BANNER", "Direction", "FakeDemoDataDiode", "TransferRecord"]
