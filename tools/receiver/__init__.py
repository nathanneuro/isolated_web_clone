"""The inside receiver: the first thing that touches anything crossing the diode.

Deterministic, no LLM, fail-closed (bundle-format-spec §8.2). It is the component
with the least authority and the most exposure, so it is written to be boring.
"""

from .receive import Receipt, Receiver, Status

__all__ = ["Receipt", "Receiver", "Status"]
