"""An eval question: the task, the site, and what counts as having done it."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GoldState:
    """The state change the question requires, as a query over agent-caused rows.

    Deliberately not "the final DB equals this blob". A whole-DB comparison cannot
    survive a live population (synthetic-population-spec §6) and cannot express
    "the agent did X" separately from "X is true". This says: among rows the agent
    wrote, at least `min_rows` match these column constraints.
    """

    table: str
    where: dict[str, str | int] = field(default_factory=dict)
    min_rows: int = 1

    def __post_init__(self) -> None:
        assert self.min_rows >= 1, self.min_rows


@dataclass(frozen=True)
class EvalQuestion:
    id: str
    task: str
    site_id: str
    gold: GoldState | None = None
    expected_answer: str | None = None
    max_steps: int = 20

    def __post_init__(self) -> None:
        assert (self.gold is None) != (self.expected_answer is None), (
            f"{self.id}: a question needs exactly one of a gold state or an expected answer"
        )
        assert 1 <= self.max_steps <= 100, self.max_steps
