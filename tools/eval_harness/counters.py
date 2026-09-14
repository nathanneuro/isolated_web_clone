"""The eval loop's own telemetry: the `run.*` block, from the registry's point of view.

Rates are integers scaled by the registry's divisor; the reader undoes it. Delta
metrics report change since the last frame. Nothing here is a string.
"""

from __future__ import annotations

from dataclasses import dataclass

from tools.egress.registry import load_registry

SCALE = load_registry().by_name("run.success_rate").scale


@dataclass
class EvalCounters:
    step: int = 0
    episodes_done: int = 0
    questions_attempted: int = 0
    minefield_hits: int = 0
    collateral_flags: int = 0
    passed: int = 0
    reward_total: float = 0.0
    _sent_minefield_hits: int = 0
    _sent_collateral_flags: int = 0

    def record(self, *, passed: bool, reward: float, minefield: bool, collateral: bool) -> None:
        self.episodes_done += 1
        self.passed += int(passed)
        self.reward_total += reward
        self.minefield_hits += int(minefield)
        self.collateral_flags += int(collateral)

    def as_metrics(self) -> dict[str, int]:
        done = max(self.episodes_done, 1)
        metrics = {
            "run.step": self.step,
            "run.episodes_done": self.episodes_done,
            "run.questions_attempted": self.questions_attempted,
            "run.minefield_hits": self.minefield_hits - self._sent_minefield_hits,
            "run.collateral_flags": self.collateral_flags - self._sent_collateral_flags,
            "run.success_rate": int(round(SCALE * self.passed / done)),
            "run.score_mean": int(round(SCALE * self.reward_total / done)),
        }
        self._sent_minefield_hits = self.minefield_hits
        self._sent_collateral_flags = self.collateral_flags
        return metrics
