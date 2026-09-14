"""The sever authority, held apart from the brokers the agent zone talks to.

The watchdog cuts the agent off by flipping this gate. The brokers consult it and
never expose it: an agent that could reach a broker's `sever` method could also
reach an `unsever`, and the agent-facing surface is `act`, `counters`, and
`model_id`, nothing more (tests/test_action_broker.py::TestOpacity).

One way. There is no method that reopens a gate; a halted run is cleared by a
human at the terminal building a new one.
"""

from __future__ import annotations


class BrokerGate:
    def __init__(self) -> None:
        self.severed = False

    def sever(self) -> None:
        self.severed = True
