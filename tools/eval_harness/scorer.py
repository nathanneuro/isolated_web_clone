"""State-diff scoring, with everything that is not the agent excluded.

design-plan §3.3 scores on final DB state. synthetic-population-spec §6 adds the
constraint that makes that survive a live environment: **animation must not be able
to change an episode's score.** Every write to a composed site names its writer
(compose_fastapi_sqlite_v1.WRITER_COLUMN), and this scorer counts only rows the
agent's own path wrote. Seed rows, driver rows, and go-live's test rows are all
somebody else's.

The credit test is positive, not negative: a row counts only if it says `agent`,
never because it failed to say anything else. A table with no writer column cannot
be scored at all, which is a loud failure rather than a quietly inflated number.

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

from tools.compose_fastapi_sqlite_v1 import WRITER_COLUMN

from .question import EvalQuestion, GoldState

AGENT = "agent"


def count_agent_rows(db_path: str, gold: GoldState) -> int:
    """Count rows matching the gold state that the agent, and nobody else, wrote."""
    assert gold.table.isidentifier(), f"unsafe table: {gold.table}"
    clauses, params = [f"{WRITER_COLUMN} = ?"], [AGENT]
    for column, value in gold.where.items():
        assert column.isidentifier(), f"unsafe column: {column}"
        clauses.append(f"{column} = ?")
        params.append(value)

    db = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({gold.table})")}
        assert WRITER_COLUMN in columns, (
            f"{gold.table} has no {WRITER_COLUMN} column; the site cannot attribute "
            f"writes, so it cannot be scored"
        )
        return db.execute(
            f"SELECT COUNT(*) FROM {gold.table} WHERE {' AND '.join(clauses)}", params
        ).fetchone()[0]
    finally:
        db.close()


def normalise_answer(text: str) -> str:
    return " ".join(text.split()).strip().lower()


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
                    f"{matched} agent-written row(s) matching gold; "
                    f"needed {question.gold.min_rows}"
                ),
                metadata={"agent_rows": matched, "steps": state.metadata["steps"]},
            )

        said = normalise_answer(state.output.completion or "")
        want = normalise_answer(question.expected_answer or "")
        return Score(
            value=CORRECT if said == want else INCORRECT,
            answer=state.output.completion or "",
            explanation=f"expected {want!r}, got {said!r}",
            metadata={"steps": state.metadata["steps"]},
        )

    return score
