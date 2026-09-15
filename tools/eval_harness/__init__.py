"""Eval harness, built on Inspect AI.

Inspect is the standard for this, so the environment plugs into it rather than
replacing it: questions are an Inspect `Dataset`, the episode loop is a `Solver`,
and scoring is a `Scorer`. What is specific to this design is what the solver is
allowed to touch.

The agent never reaches a model provider and never reaches the site. It reaches the
action broker and the env broker, which is the same constraint it would be under
inside the airgap. Running the standard framework does not widen the agent's
authority, because the brokers are what the framework talks to.
"""

from .counters import EvalCounters
from .question import EvalQuestion, GoldState, Milestone, Minefield
from .scorer import reward_scorer, state_diff_scorer
from .solver import broker_web_agent
from .task import MultiSiteEnvFactory, SiteEnvFactory, build_task

__all__ = [
    "EvalCounters",
    "EvalQuestion",
    "GoldState",
    "Milestone",
    "Minefield",
    "MultiSiteEnvFactory",
    "SiteEnvFactory",
    "broker_web_agent",
    "build_task",
    "reward_scorer",
    "state_diff_scorer",
]
