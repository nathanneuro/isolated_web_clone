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

from tools.brokers import ActionBroker, ActionKind, EnvBroker, observation_from

from .question import EvalQuestion


@solver
def broker_web_agent(
    action_broker: ActionBroker,
    env_factory,
    questions: dict[str, EvalQuestion],
) -> Solver:
    """Drive one episode per sample through the two brokers.

    `env_factory(question)` returns a fresh `EnvBroker` over a freshly reset site,
    because per-episode reset is what makes episodes comparable.
    """

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        question = questions[state.sample_id]
        env: EnvBroker = env_factory(question)
        view = env.observe()

        transcript: list[dict] = []
        answer = ""

        for step in range(question.max_steps):
            observation = observation_from(
                view, question.task, step, action_broker.counters
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

        state.metadata["transcript"] = transcript
        state.metadata["steps"] = len(transcript)
        state.metadata["env_counters"] = vars(env.counters)
        state.metadata["broker_counters"] = action_broker.counters.as_metrics()
        state.metadata["db_path"] = str(env_factory.db_path_for(question))
        state.output.completion = answer
        return state

    return solve
