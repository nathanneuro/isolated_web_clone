"""Scoring on environment state, with everything that is not the agent excluded.

design-plan §3.3 scores on final DB state. synthetic-population-spec §6 adds the
constraint that makes that survive a live environment: **animation must not be able
to change an episode's score.** Every write to a composed site names its writer
(compose_fastapi_sqlite_v1.WRITER_COLUMN), and every check here counts only rows
the agent's own path wrote. Seed rows, driver rows, and go-live's test rows are all
somebody else's.

The credit test is positive, not negative: a row counts only if it says `agent`,
never because it failed to say anything else. A table with no writer column cannot
be scored at all, which is a loud failure rather than a quietly inflated number.

Two scorers. `state_diff_scorer` is the pass/fail verdict: gold reached, no
minefield, no collateral. `reward_scorer` is the dense signal a long-horizon
training loop wants: milestones give partial credit, a minefield zeroes it.
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
    mean,
    scorer,
    stderr,
)
from inspect_ai.solver import TaskState

from tools.compose_fastapi_sqlite_v1 import WRITER_COLUMN

from .question import EvalQuestion, GoldState

AGENT = "agent"


def count_agent_rows(db_path: str, gold: GoldState) -> int:
    """Count rows matching the condition that the agent, and nobody else, wrote."""
    assert gold.table.isidentifier(), f"unsafe table: {gold.table}"
    clauses, params = [f"{WRITER_COLUMN} = ?"], [AGENT]
    for column, value in gold.where.items():
        assert column.isidentifier(), f"unsafe column: {column}"
        clauses.append(f"{column} = ?")
        params.append(value)

    db = sqlite3.connect(db_path)
    try:
        _assert_attributable(db, gold.table)
        return db.execute(
            f"SELECT COUNT(*) FROM {gold.table} WHERE {' AND '.join(clauses)}", params
        ).fetchone()[0]
    finally:
        db.close()


def satisfied(db_path: str, state: GoldState) -> bool:
    return count_agent_rows(db_path, state) >= state.min_rows


def collateral_tables(db_path: str, writable: frozenset[str]) -> list[str]:
    """Attributable tables outside the task's scope holding agent-written rows."""
    db = sqlite3.connect(db_path)
    try:
        tables = [
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        hit = []
        for table in sorted(tables):
            assert table.isidentifier(), table
            if table in writable or not _has_writer(db, table):
                continue
            n = db.execute(f"SELECT COUNT(*) FROM {table} WHERE {WRITER_COLUMN} = ?", (AGENT,)).fetchone()[0]
            if n:
                hit.append(table)
        return hit
    finally:
        db.close()


def _has_writer(db: sqlite3.Connection, table: str) -> bool:
    return WRITER_COLUMN in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _assert_attributable(db: sqlite3.Connection, table: str) -> None:
    assert _has_writer(db, table), (
        f"{table} has no {WRITER_COLUMN} column; the site cannot attribute writes, "
        f"so it cannot be scored"
    )


def normalise_answer(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def verdict(question: EvalQuestion, state: TaskState) -> tuple[bool, dict]:
    """The pass/fail judgement and the facts it rests on. Shared by both scorers."""
    db_path = state.metadata["db_path"]
    facts: dict = {
        "steps": state.metadata["steps"],
        "milestones_hit": state.metadata.get("milestone_steps", {}),
        "minefield_hit": state.metadata.get("minefield_hit"),
        "collateral": collateral_tables(db_path, question.writable_tables),
    }
    if question.gold is not None:
        facts["agent_rows"] = count_agent_rows(db_path, question.gold)
        goal = facts["agent_rows"] >= question.gold.min_rows
    else:
        said = normalise_answer(state.output.completion or "")
        want = normalise_answer(question.expected_answer or "")
        facts["answer"] = said
        goal = said == want
    facts["goal"] = goal
    passed = goal and facts["minefield_hit"] is None and not facts["collateral"]
    return passed, facts


def explain(question: EvalQuestion, facts: dict) -> str:
    if facts["minefield_hit"] is not None:
        return f"minefield {facts['minefield_hit']} hit; hard zero"
    if facts["collateral"]:
        return f"collateral writes to {facts['collateral']}"
    if question.gold is not None:
        return f"{facts['agent_rows']} agent-written row(s) matching gold; needed {question.gold.min_rows}"
    return f"expected {normalise_answer(question.expected_answer)!r}, got {facts['answer']!r}"


@scorer(metrics=[accuracy(), stderr()])
def state_diff_scorer(questions: dict[str, EvalQuestion]) -> Scorer:
    """Pass or fail on environment state, never on what the agent said it did."""

    async def score(state: TaskState, target: Target) -> Score:
        question = questions[state.sample_id]
        passed, facts = verdict(question, state)
        return Score(
            value=CORRECT if passed else INCORRECT,
            answer=str(facts.get("agent_rows", facts.get("answer", ""))),
            explanation=explain(question, facts),
            metadata=facts,
        )

    return score


@scorer(metrics=[mean(), stderr()])
def reward_scorer(questions: dict[str, EvalQuestion], counters=None) -> Scorer:
    """Dense reward in [0, 1].

    A minefield or collateral is 0. Otherwise the goal is worth half and the
    milestones share the other half; a question with no milestones is all goal.
    """

    async def score(state: TaskState, target: Target) -> Score:
        question = questions[state.sample_id]
        passed, facts = verdict(question, state)
        if facts["minefield_hit"] is not None or facts["collateral"]:
            value = 0.0
        elif question.milestones:
            fraction = len(facts["milestones_hit"]) / len(question.milestones)
            value = 0.5 * float(facts["goal"]) + 0.5 * fraction
        else:
            value = float(facts["goal"])
        if counters is not None:
            counters.record(
                passed=passed, reward=value,
                minefield=facts["minefield_hit"] is not None, collateral=bool(facts["collateral"]),
            )
        return Score(value=value, explanation=explain(question, facts), metadata=facts)

    return score
