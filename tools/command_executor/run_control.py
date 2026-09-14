"""Run control: the state the eval loop polls, and the only thing commands may move.

Commands start and stop runs. The escape watchdog halts them. The loop itself
reports progress. Nothing in the agent zone can reach this object, and a run that
the watchdog halted cannot be restarted by a command: only a human at the terminal
clears the halt (agent-sandbox-spec §5.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class RunState(IntEnum):
    """metrics-registry `run.state`: 0 idle, 1 running, 2 paused, 3 done, 4 error."""

    IDLE = 0
    RUNNING = 1
    PAUSED = 2
    DONE = 3
    ERROR = 4


STARTABLE = frozenset({RunState.IDLE, RunState.DONE, RunState.ERROR})


@dataclass
class RunControl:
    state: RunState = RunState.IDLE
    run_id: str = ""
    config_ref: str = ""
    params: dict = field(default_factory=dict)
    halted: bool = False  # set by the watchdog; cleared only at the terminal
    quarantined_checkpoints: int = 0

    def start(self, run_id: str, config_ref: str, params: dict) -> None:
        assert not self.halted, "run loop is halted by the watchdog; clear it at the terminal"
        assert self.state in STARTABLE, self.state
        self.state, self.run_id, self.config_ref, self.params = RunState.RUNNING, run_id, config_ref, dict(params)

    def stop(self) -> None:
        assert self.state in (RunState.RUNNING, RunState.PAUSED), self.state
        self.state = RunState.DONE

    def halt(self, *, quarantine_checkpoint: bool) -> None:
        """The watchdog's entry point. Idempotent; never raises."""
        self.halted = True
        if self.state in (RunState.RUNNING, RunState.PAUSED):
            self.state = RunState.ERROR
        if quarantine_checkpoint:
            self.quarantined_checkpoints += 1

    def clear_halt_at_terminal(self) -> None:
        """Only a human at the wired terminal. Nothing automated calls this."""
        self.halted = False

    def as_metrics(self) -> dict[str, int]:
        return {"run.state": int(self.state)}
