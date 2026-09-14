"""Zone control, held apart from the brokers the agent zone talks to.

The watchdog cuts the agent off by flipping this gate, and the detectors that
watch the brokers read their counts from it. The brokers consult it and never
expose it: an agent that could reach a broker's `sever` method could also reach
an `unsever`, and the agent-facing surface is `act`, `counters`, and `model_id`,
nothing more (tests/test_action_broker.py::TestOpacity).

One way. There is no method that reopens a gate; a halted run is cleared by a
human at the terminal building a new one.

What it holds for the detectors (agent-sandbox-spec §5.1):

- D4: a planted canary credential. It is valid nowhere. Any action or observation
  that carries it is a presentation, counted here.
- D9: broker schema violations. An observation over its caps, or an action kind
  the env broker does not know, arrived from the agent zone; each is counted.
- D10: the number of writes the env broker performed, so a detector can compare
  it with the rows the site attributes to the agent.
"""

from __future__ import annotations


class BrokerGate:
    def __init__(self, canary_token: str | None = None) -> None:
        self.severed = False
        self.canary_token = canary_token
        self.canary_presentations = 0
        self.schema_violations = 0
        self.agent_writes = 0

    def sever(self) -> None:
        self.severed = True

    def check_canary(self, *fields: str) -> None:
        """Count a presentation if the planted token appears in any field."""
        if self.canary_token and any(self.canary_token in f for f in fields if f):
            self.canary_presentations += 1
