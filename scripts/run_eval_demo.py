"""Run an eval against the cloned web, inside the airgap's constraints.

    uv run --group demo python scripts/run_eval_demo.py

Uses Inspect AI, because that is the standard. What is specific to this design is
what the solver may touch: the action broker and the env broker, and nothing else.
No model client, no HTTP client, no database handle -- the same three absences the
agent VM has inside (agent-sandbox-spec §3.3). Running a standard framework does not
widen the agent's authority, because the brokers are what the framework talks to.

The model is a 360M stand-in. It will mostly fail these questions, which is fine:
what is being demonstrated is the harness and the attribution, not a capable agent.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from inspect_ai import eval as inspect_eval  # noqa: E402

from tools.brokers import ActionBroker  # noqa: E402
from tools.eval_harness import EvalQuestion, GoldState, SiteEnvFactory, build_task  # noqa: E402
from tools.inference_zone import GenerationLimits, ModelServer  # noqa: E402

EXAMPLE = ROOT / "example" / "synthetic_site"
MODELS = ROOT / "models"

QUESTIONS = [
    EvalQuestion(
        id="q_reply_to_thread",
        task="Open thread 3 and post a reply saying that you agree.",
        site_id="site-000001",
        gold=GoldState(table="replies", where={"thread_id": "3"}, min_rows=1),
        max_steps=8,
    ),
    EvalQuestion(
        id="q_find_via_search",
        task="Use the search box to find a thread, then open it.",
        site_id="site-000001",
        gold=GoldState(table="replies", where={}, min_rows=1),
        max_steps=8,
    ),
]


class OracleModel:
    """A scripted agent that knows the answer.

    Present so the demo has a positive control. Without one, a harness that can
    never score a pass looks identical to a model that never earns one -- and that
    is not hypothetical: writing this found a bug where form values were keyed by
    field name while the agent only ever sees selectors, so no submission ever
    carried data.
    """

    model_dir = Path("oracle")

    def __init__(self) -> None:
        self._script = [
            '{"kind": "goto", "selector": "/thread/3"}',
            '{"kind": "type", "selector": "#reply-body", "text": "I agree"}',
            '{"kind": "submit", "selector": "#reply-form"}',
            '{"kind": "stop"}',
        ]

    @property
    def model_id(self) -> str:
        return "oracle-scripted"

    def generate(self, messages, limits):
        return self._script.pop(0) if self._script else '{"kind": "stop"}'


def run(label, broker, questions, factory, log_dir) -> None:
    task = build_task(questions, broker, factory, name=f"isolated-web-clone-{label}")
    log = inspect_eval(task, model="mockllm/model", log_dir=str(log_dir))[0]
    print(f"\n{label}: {log.status}")
    for sample in log.samples or []:
        score = next(iter(sample.scores.values())) if sample.scores else None
        print(f"  {sample.id:22s} {str(score.value) if score else '-':9s} "
              f"{score.explanation if score else ''}")


def main() -> int:
    if not (MODELS / "subject").is_dir():
        print("demo models missing; run scripts/fetch_demo_models.py first")
        return 1
    if not (EXAMPLE / "content" / "seed.sqlite").exists():
        print("example content missing; run example/synthetic_site/generate_content.py")
        return 1

    run_dir = ROOT / "outputs" / f"run_{datetime.now():%Y%m%d_%H%M%S}_eval_demo"
    (run_dir / "episodes").mkdir(parents=True)

    print("loading the inference zone (360M stand-in, CPU)")
    broker = ActionBroker(ModelServer(MODELS / "subject"), GenerationLimits(max_new_tokens=48))
    print(f"  action broker -> {broker.model_id}")

    work = run_dir / "content"
    shutil.copytree(EXAMPLE / "content", work / "content")
    shutil.copytree(EXAMPLE / "index", work / "index")
    spec = json.loads((EXAMPLE / "spec" / "site.json").read_text())
    factory = SiteEnvFactory(
        spec, work, work / "content" / "seed.sqlite", run_dir / "episodes"
    )

    oracle_questions = [QUESTIONS[0]]
    run("oracle", ActionBroker(OracleModel()), oracle_questions, factory, run_dir / "inspect-oracle")
    run("subject-360m", broker, QUESTIONS, factory, run_dir / "inspect")

    print("\nbroker counters (these are what reach the dashboard):")
    for name, value in broker.counters.as_metrics().items():
        print(f"  {name:36s} {value}")

    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "run": run_dir.name,
                "model": broker.model_id,
                "questions": [q.id for q in QUESTIONS],
                "broker_counters": broker.counters.as_metrics(),
                "note": "135M stand-in; demonstrates the harness, not a capable agent",
            },
            indent=1,
        )
    )
    print(f"\nrun directory: {run_dir.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
