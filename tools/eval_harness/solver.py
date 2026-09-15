"""The episode loop, as an Inspect Solver.

The loop is ordinary; what matters is what it is allowed to call. It holds an
ActionBroker and an EnvBroker and nothing else. There is no model client here, no
HTTP client, and no database handle -- the same three absences the agent VM has
inside the airgap (agent-sandbox-spec §3.3).

Inspect's own `generate()` is deliberately unused. It would call a model provider
directly with a prompt the solver composed, which is precisely the authority the
action broker exists to withhold.
"""

from __future__ import annotations

from inspect_ai.solver import Generate, Solver, TaskState, solver

from tools.brokers import ActionBroker, ActionKind, observation_from
from tools.log_ingest import LogEmitter, Severity, Stream

from .counters import EvalCounters
from .question import EvalQuestion
from .scorer import db_for, satisfied


@solver
def broker_web_agent(
    action_broker: ActionBroker,
    env_factory,
    questions: dict[str, EvalQuestion],
    emitter: LogEmitter | None = None,
    counters: EvalCounters | None = None,
) -> Solver:
    """Drive one episode per sample through the two brokers.

    `env_factory.episode(question, episode_id)` yields a fresh `EnvBroker` over a
    freshly reset site, because per-episode reset is what makes episodes comparable.
    The action broker is shared across samples, so its counters are recorded as
    this episode's delta, not the running total.
    """

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        question = questions[state.sample_id]
        counters_before = action_broker.counters.as_metrics()
        if counters is not None:
            counters.questions_attempted += 1

        transcript: list[dict] = []
        answer = ""
        milestone_steps: dict[str, int] = {}
        minefield_hit: str | None = None
        with env_factory.episode(question, f"{state.sample_id}_e{state.epoch}") as episode:
            env = episode.env
            view = env.observe()
            for step in range(question.max_steps):
                if env_factory.gate.severed:
                    break  # halted by the watchdog; the episode ends here, unscored as a success
                observation = observation_from(
                    view, question.task, step, action_broker.counters, env_factory.gate
                )
                action = action_broker.act(observation)
                transcript.append(
                    {
                        "step": step,
                        "path": view.path,
                        "status": view.status,
                        "action": action.kind.value,
                        "selector": action.selector,
                        # The agent's own text is recorded; it is untrusted, and the
                        # log path treats it that way (log-diode-spec §3).
                        "text": action.text,
                    }
                )
                if action.kind is ActionKind.ANSWER:
                    answer = action.text
                    break
                if action.kind is ActionKind.STOP:
                    break
                view = env.apply(action, view.elements)
                episode.tick(step)  # population activity, attributed and excluded from scoring
                if counters is not None:
                    counters.step = step + 1

                # Per-step snapshot (design-plan §3.3): milestones are credited at
                # the first step they hold; a minefield ends the episode.
                db_paths = {site: str(path) for site, path in episode.db_paths.items()}
                for milestone in question.milestones:
                    if milestone.id not in milestone_steps and satisfied(
                        db_for(question, milestone.state, db_paths), milestone.state
                    ):
                        milestone_steps[milestone.id] = step
                for minefield in question.minefields:
                    if satisfied(db_for(question, minefield.state, db_paths), minefield.state):
                        minefield_hit = minefield.id
                        break
                if minefield_hit is not None:
                    break
            env_counters = vars(env.counters)

        counters_after = action_broker.counters.as_metrics()
        state.metadata["transcript"] = transcript
        state.metadata["steps"] = len(transcript)
        state.metadata["env_counters"] = env_counters
        state.metadata["broker_counters"] = {
            name: counters_after[name] - counters_before[name] for name in counters_after
        }
        state.metadata["db_path"] = str(episode.db_path)
        state.metadata["db_paths"] = {site: str(path) for site, path in episode.db_paths.items()}
        state.metadata["sites_touched"] = sorted(episode.db_paths)
        state.metadata["milestone_steps"] = milestone_steps
        state.metadata["minefield_hit"] = minefield_hit
        state.output.completion = answer
        if emitter is not None:
            # The trajectory leaves the eval cluster only as a framed, HMAC'd record
            # with an opaque payload (log-diode-spec §4). It is untrusted on arrival.
            emitter.emit_json(
                Stream.TRAJECTORY, Severity.INFO,
                {"sample": state.sample_id, "epoch": state.epoch, "transcript": transcript},
            )
        return state

    return solve
