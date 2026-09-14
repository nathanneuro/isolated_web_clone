"""Run an eval against the cloned web, inside the airgap's constraints.

    uv run --group demo python scripts/run_eval_demo.py

Uses Inspect AI, because that is the standard. What is specific to this design is
what the solver may touch: the action broker and the env broker, and nothing else.
No model client, no HTTP client, no database handle -- the same three absences the
agent VM has inside (agent-sandbox-spec §3.3). Running a standard framework does not
widen the agent's authority, because the brokers are what the framework talks to.

The model is a sub-1B stand-in (whichever scripts/fetch_demo_models.py placed under
models/subject). It will mostly fail these questions, which is fine: what is being
demonstrated is the harness and the attribution, not a capable agent.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
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
SEED_DB = EXAMPLE / "content" / "seed.sqlite"


def questions() -> list[EvalQuestion]:
    """One state-diff question and one answer question, so both scorer paths run.

    The expected answer is read from the seed so the question stays true when the
    example content is regenerated.
    """
    author = sqlite3.connect(SEED_DB).execute(
        "SELECT author FROM threads WHERE id = 3"
    ).fetchone()[0]
    return [
        EvalQuestion(
            id="q_reply_to_thread",
            task="Open thread 3 and post a reply saying that you agree.",
            site_id="site-000001",
            gold=GoldState(table="replies", where={"thread_id": 3}, min_rows=1),
            max_steps=8,
        ),
        EvalQuestion(
            id="q_thread_author",
            task="Open thread 3 and answer with the username of its author.",
            site_id="site-000001",
            expected_answer=author,
            max_steps=8,
        ),
    ]


def main() -> int:
    if not (MODELS / "subject").is_dir():
        print("demo models missing; run scripts/fetch_demo_models.py first")
        return 1
    if not SEED_DB.exists():
        print("example content missing; run example/synthetic_site/generate_content.py")
        return 1

    run_dir = ROOT / "outputs" / f"run_{datetime.now():%Y%m%d_%H%M%S}_eval_demo"
    (run_dir / "episodes").mkdir(parents=True)

    print("loading the inference zone (CPU)")
    broker = ActionBroker(ModelServer(MODELS / "subject"), GenerationLimits(max_new_tokens=48))
    print(f"  action broker -> {broker.model_id}")

    work = run_dir / "content"
    shutil.copytree(EXAMPLE / "content", work / "content")
    shutil.copytree(EXAMPLE / "index", work / "index")
    spec = json.loads((EXAMPLE / "spec" / "site.json").read_text())
    factory = SiteEnvFactory(spec, work, work / "content" / "seed.sqlite", run_dir / "episodes")

    qs = questions()
    task = build_task(qs, broker, factory)
    log = inspect_eval(task, model="mockllm/model", log_dir=str(run_dir / "inspect"))[0]

    print(f"\nstatus: {log.status}")
    if log.status != "success":
        print(log.error)
        return 1
    for sample in log.samples:
        score = sample.scores["state_diff_scorer"]
        print(f"  {sample.id:22s} {score.value:3s} {score.explanation}")

    print("\nbroker counters (these are what reach the dashboard):")
    for name, value in broker.counters.as_metrics().items():
        print(f"  {name:36s} {value}")

    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "run": run_dir.name,
                "model": broker.model_id,
                "questions": [q.id for q in qs],
                "scores": {s.id: s.scores["state_diff_scorer"].value for s in log.samples},
                "broker_counters": broker.counters.as_metrics(),
                "note": "sub-1B stand-in; demonstrates the harness, not a capable agent",
            },
            indent=1,
        )
    )
    print(f"\nrun directory: {run_dir.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
