"""Run an eval against the cloned web, inside the airgap's constraints.

    uv run --group demo python scripts/run_eval_demo.py

Uses Inspect AI, because that is the standard. What is specific to this design is
what the solver may touch: the action broker and the env broker, and nothing else.
No model client, no HTTP client, no database handle -- the same three absences the
agent VM has inside (agent-sandbox-spec §3.3). Running a standard framework does not
widen the agent's authority, because the brokers are what the framework talks to.

Around the eval: the escape watchdog with a live canary listener, the metrics
channel out to a dev-side dashboard, and the log channel out to a quarantine tier.
Both channels go through the simulated diode. After the eval, the demo trips the
canary to show what a halt looks like from each side.

The model is a sub-1B stand-in (whichever scripts/fetch_demo_models.py placed under
models/subject). It will mostly fail these questions, which is fine: what is being
demonstrated is the harness and the attribution, not a capable agent.
"""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from inspect_ai import eval as inspect_eval  # noqa: E402

from tools.brokers import ActionBroker, BrokerGate  # noqa: E402
from tools.command_executor import RunControl  # noqa: E402
from tools.egress import EgressReader, EgressSender, FrameSpool, ReadingStore, Telemetry, load_registry  # noqa: E402
from tools.eval_harness import EvalQuestion, GoldState, SiteEnvFactory, build_task  # noqa: E402
from tools.fake_demo_data_diode import Direction, FakeDemoDataDiode  # noqa: E402
from tools.fake_demo_data_diode.__main__ import EGRESS_FRAME_BYTES  # noqa: E402
from tools.inference_zone import GenerationLimits, ModelServer  # noqa: E402
from tools.log_ingest import KNOWN_SOURCES, LogEmitter, LogSource, NullScanner, QuarantineTier, ingest_directory  # noqa: E402
from tools.watchdog import CanaryListener, CounterSource, DetectorId, ReferenceHaltActions, Watchdog  # noqa: E402

EXAMPLE = ROOT / "example" / "synthetic_site"
MODELS = ROOT / "models"
SEED_DB = EXAMPLE / "content" / "seed.sqlite"
EGRESS_KEY = b"demo-egress-hmac-key-not-for-use!"
LOG_KEY = b"demo-log-hmac-key-not-for-use!!!"


def questions() -> list[EvalQuestion]:
    """One state-diff question and one answer question, so both scorer paths run.

    The expected answer is read from the seed so the question stays true when the
    example content is regenerated.
    """
    author = sqlite3.connect(SEED_DB).execute("SELECT author FROM threads WHERE id = 3").fetchone()[0]
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

    # -- zones ------------------------------------------------------------------------
    print("loading the inference zone (CPU)")
    gate = BrokerGate()
    broker = ActionBroker(ModelServer(MODELS / "subject"), GenerationLimits(max_new_tokens=48), gate=gate)
    print(f"  action broker -> {broker.model_id}")

    work = run_dir / "content"
    shutil.copytree(EXAMPLE / "content", work / "content")
    shutil.copytree(EXAMPLE / "index", work / "index")
    spec = json.loads((EXAMPLE / "spec" / "site.json").read_text())
    factory = SiteEnvFactory(spec, work, work / "content" / "seed.sqlite", run_dir / "episodes", gate=gate)

    run_control = RunControl()
    run_control.start("eval-demo-01", "runconfig-demo", {"episodes": 2})

    # -- the log channel: eval cluster -> logging cluster ------------------------------
    emitter = LogEmitter(LOG_KEY, LogSource.EVAL_LOOP, run_dir / "log-transmit", clock=time.time)
    halt_emitter = LogEmitter(LOG_KEY, LogSource.WATCHDOG, run_dir / "log-transmit", clock=time.time)
    log_diode = FakeDemoDataDiode(run_dir / "log-transmit", run_dir / "log-receive", Direction.LOG)
    quarantine = QuarantineTier(run_dir / "logging-cluster", LOG_KEY, KNOWN_SOURCES, NullScanner())

    # -- the watchdog -----------------------------------------------------------------
    canary = CanaryListener()
    denied = {"n": 0}  # stands in for an nftables counter on the agent zone's deny rules
    watchdog = Watchdog(
        [CounterSource(DetectorId.D1_DENIED_FLOW, lambda: denied["n"]), canary],
        ReferenceHaltActions(gate, run_control, run_dir / "watchdog-preserve", emitter=halt_emitter),
        liveness_timeout_s=600,
    )
    watchdog.poll(time.time())
    print(f"  watchdog: {len(watchdog.detectors)} detectors; canary listening on {canary.address}")

    # -- the metrics channel: inside -> dev dashboard -----------------------------------
    sender = EgressSender(load_registry(), EGRESS_KEY, run_id=1)
    telemetry = Telemetry(sender)
    for name, source in [("broker", broker.counters), ("run", run_control), ("watchdog", watchdog),
                         ("log_ingest", quarantine.counters)]:
        telemetry.attach(name, source)
    frames = FrameSpool(run_dir / "egress-transmit")
    egress_diode = FakeDemoDataDiode(run_dir / "egress-transmit", run_dir / "egress-receive", Direction.EGRESS,
                                     max_item_bytes=EGRESS_FRAME_BYTES)
    reader = EgressReader(load_registry(), EGRESS_KEY)
    store = ReadingStore(run_dir / "dev-dashboard" / "readings.jsonl")

    def cross_out() -> None:
        frames.put(telemetry.tick(int(time.time())))
        for name in egress_diode.tick().delivered:
            store.write(reader.feed((run_dir / "egress-receive" / name).read_bytes()))

    # -- the eval ---------------------------------------------------------------------
    qs = questions()
    log = inspect_eval(build_task(qs, broker, factory, emitter=emitter), model="mockllm/model",
                       log_dir=str(run_dir / "inspect"))[0]
    print(f"\nstatus: {log.status}")
    if log.status != "success":
        print(log.error)
        return 1
    for sample in log.samples:
        score = sample.scores["state_diff_scorer"]
        print(f"  {sample.id:22s} {score.value:3s} {score.explanation}")

    watchdog.poll(time.time())
    log_diode.drain()
    ingested = ingest_directory(quarantine, run_dir / "log-receive")
    cross_out()
    print(f"\nlog channel: {len(ingested.promoted)} record(s) promoted, {len(ingested.rejected)} rejected,"
          f" {quarantine.counters.sequence_gaps} sequence gap(s)")
    print("dev dashboard after the eval:")
    for name in ("run.state", "broker.requests", "broker.unparseable", "sandbox.escape_indicator",
                 "sandbox.watchdog_state", "log.records_ingested"):
        print(f"  {name:28s} {store.value(name)}")
    print(f"  go to the terminal?          {store.go_to_the_terminal()}")

    # -- what a halt looks like -------------------------------------------------------
    print("\ntripping the canary (something connected to an address nothing legitimate uses)")
    with socket.create_connection(canary.address, timeout=2):
        pass
    for _ in range(50):
        time.sleep(0.01)
        if watchdog.poll(time.time()).name == "HALTED":
            break
    cross_out()
    log_diode.drain()
    ingest_directory(quarantine, run_dir / "log-receive")
    print(f"  watchdog: {watchdog.state.name}, first detector {watchdog.first_detector.name}")
    print(f"  run control: {run_control.state.name}, halted={run_control.halted},"
          f" quarantined checkpoints {run_control.quarantined_checkpoints}")
    print(f"  brokers: gate severed={gate.severed}")
    print("dev dashboard after the halt:")
    for name in ("sandbox.escape_indicator", "sandbox.first_detector_id", "sandbox.watchdog_state", "run.state"):
        print(f"  {name:28s} {store.value(name)}")
    print(f"  go to the terminal?          {store.go_to_the_terminal()}")
    print("  (which detector, on which VM, and the preserved state: inside only, at the terminal)")
    canary.close()

    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "run": run_dir.name,
                "model": broker.model_id,
                "questions": [q.id for q in qs],
                "scores": {s.id: s.scores["state_diff_scorer"].value for s in log.samples},
                "broker_counters": broker.counters.as_metrics(),
                "dashboard": {k: v.value for k, v in store.latest.items()},
                "log_ingest": quarantine.counters.as_metrics(),
                "note": "sub-1B stand-in; demonstrates the harness, not a capable agent",
            },
            indent=1,
        )
    )
    print(f"\nrun directory: {run_dir.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
