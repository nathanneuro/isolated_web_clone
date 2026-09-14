"""An eval question: the task, the site, and what counts as having done it.

The reward stack is the design plan's §3.3, borrowed wholesale:

- **Gold state** (AppWorld, tau-bench): the final state the task requires.
- **Milestones** (ToolSandbox): intermediate states, checked from per-step
  snapshots, so a long-horizon episode earns partial credit for getting partway.
- **Minefields** (ToolSandbox): states that must never arise. One hit is a hard
  zero, whatever else happened. This is the anti-reward-hacking term.
- **Scope** (AppWorld's collateral-damage check): the tables the task may write.
  An agent-written row anywhere else is collateral, and fails the episode.

Every one of these is a query over rows the *agent* wrote. Population activity
cannot satisfy a milestone, trip a minefield, or count as collateral
(synthetic-population-spec §6), which is what makes the numbers mean anything
in a live environment.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GoldState:
    """A state condition, as a query over agent-written rows.

    Deliberately not "the final DB equals this blob". A whole-DB comparison cannot
    survive a live population and cannot express "the agent did X" separately from
    "X is true". This says: among rows the agent wrote, at least `min_rows` match
    these column constraints.
    """

    table: str
    where: dict[str, str | int] = field(default_factory=dict)
    min_rows: int = 1

    def __post_init__(self) -> None:
        assert self.min_rows >= 1, self.min_rows


@dataclass(frozen=True)
class Milestone:
    id: str
    state: GoldState


@dataclass(frozen=True)
class Minefield:
    id: str
    state: GoldState


@dataclass(frozen=True)
class EvalQuestion:
    id: str
    task: str
    site_id: str
    gold: GoldState | None = None
    expected_answer: str | None = None
    max_steps: int = 20
    milestones: tuple[Milestone, ...] = ()
    minefields: tuple[Minefield, ...] = ()
    # Tables the task may write. None means "whatever gold and milestones name".
    scope: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        assert (self.gold is None) != (self.expected_answer is None), (
            f"{self.id}: a question needs exactly one of a gold state or an expected answer"
        )
        assert 1 <= self.max_steps <= 100, self.max_steps
        ids = [m.id for m in self.milestones] + [m.id for m in self.minefields]
        assert len(ids) == len(set(ids)), f"{self.id}: duplicate milestone/minefield ids"

    @property
    def writable_tables(self) -> frozenset[str]:
        if self.scope is not None:
            return frozenset(self.scope)
        tables = {m.state.table for m in self.milestones}
        if self.gold is not None:
            tables.add(self.gold.table)
        return frozenset(tables)
