"""State-diff scoring, with population activity excluded.

design-plan §3.3 scores on final DB state. synthetic-population-spec §6 adds the
constraint that makes that survive a live environment: **animation must not be able
to change an episode's score.** Rows a population driver created are tagged at
insert, and this scorer counts only untagged rows -- the ones that arrived through
the agent's own path.

If turning a population on or off moves measured performance, that is an attribution
bug here, not a finding about the model.
"""

from __future__ import annotations

import sqlite3

from inspect_ai.scorer import (
    CORRECT,
    INCORRECT,
    Score,
    Scorer,
    Target,
    accuracy,
    scorer,
    stderr,
)
from inspect_ai.solver import TaskState

from .question import EvalQuestion, GoldState

IDENTIFIER_OK = str.isidentifier


def count_agent_rows(db_path: str, gold: GoldState) -> int:
    """Count rows matching the gold state that the agent, not a driver, caused."""
    assert IDENTIFIER_OK(gold.table), f"unsafe table: {gold.table}"
    clauses, params = [], []
    for column, value in gold.where.items():
        assert IDENTIFIER_OK(column), f"unsafe column: {column}"
        clauses.append(f"{column} = ?")
        params.append(value)
    if gold.exclude_tagged and _has_column(db_path, gold.table, "driver_tag"):
        # NULL means "arrived through the agent's path". Seed rows are tagged
        # 'seed', driver rows carry the driver's id; neither is the agent's doing.
        clauses.append("driver_tag IS NULL")

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    db = sqlite3.connect(db_path)
    try:
        return db.execute(f"SELECT COUNT(*) FROM {gold.table}{where}", params).fetchone()[0]
    finally:
        db.close()


def _has_column(db_path: str, table: str, column: str) -> bool:
    db = sqlite3.connect(db_path)
    try:
        return any(row[1] == column for row in db.execute(f"PRAGMA table_info({table})"))
    finally:
        db.close()


@scorer(metrics=[accuracy(), stderr()])
def state_diff_scorer(questions: dict[str, EvalQuestion]) -> Scorer:
    """Score an episode on environment state, never on what the agent said it did."""

    async def score(state: TaskState, target: Target) -> Score:
        question = questions[state.sample_id]

        if question.gold is not None:
            matched = count_agent_rows(state.metadata["db_path"], question.gold)
            passed = matched >= question.gold.min_rows
            return Score(
                value=CORRECT if passed else INCORRECT,
                answer=str(matched),
                explanation=(
                    f"{matched} agent-attributable row(s) matching gold; "
                    f"needed {question.gold.min_rows}"
                ),
                metadata={"agent_rows": matched, "steps": state.metadata.get("steps")},
            )

        said = (state.output.completion or "").strip().lower()
        want = (question.expected_answer or "").strip().lower()
        passed = bool(want) and want in said
        return Score(
            value=CORRECT if passed else INCORRECT,
            answer=state.output.completion or "",
            explanation=f"expected {want!r} in answer",
            metadata={"steps": state.metadata.get("steps")},
        )

    return score
