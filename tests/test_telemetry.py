"""Both one-way channels carrying real traffic from real components.

Egress: every component's counters ride the metrics socket into frames, across
the (simulated) diode, through the reader, into the dev-side store. Log: records
from the eval side cross the log diode into quarantine and come out promoted, and
a record that never arrives shows up as a sequence gap.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tools.brokers import ActionBroker, BrokerGate
from tools.command_executor import RunControl
from tools.egress import EgressReader, EgressSender, FrameSpool, ReadingStore, Telemetry, load_registry
from tools.eval_harness import EvalQuestion, GoldState, SiteEnvFactory, build_task
from tools.fake_demo_data_diode import Direction, FakeDemoDataDiode
from tools.fake_demo_data_diode.__main__ import EGRESS_FRAME_BYTES
from tools.log_ingest import (
    KNOWN_SOURCES,
    LogEmitter,
    LogSource,
    NullScanner,
    QuarantineTier,
    Severity,
    Stream,
    ingest_directory,
)
from tools.registry import SiteRegistry
from tools.watchdog import CounterSource, DetectorId, ReferenceHaltActions, Watchdog

EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"
KEY = b"k" * 32


class ScriptedModel:
    model_id = "scripted"
    model_dir = Path("scripted")

    def generate(self, messages, limits):
        return '{"kind": "stop"}'


def egress_link(tmp_path):
    registry = load_registry()
    sender = EgressSender(registry, KEY, run_id=7)
    telemetry = Telemetry(sender)
    spool = FrameSpool(tmp_path / "tx")
    diode = FakeDemoDataDiode(tmp_path / "tx", tmp_path / "rx", Direction.EGRESS, max_item_bytes=EGRESS_FRAME_BYTES)
    reader = EgressReader(registry, KEY)
    store = ReadingStore(tmp_path / "dev" / "readings.jsonl")

    def cross(timestamp: int) -> None:
        spool.put(telemetry.tick(timestamp))
        for name in diode.tick().delivered:
            store.write(reader.feed((tmp_path / "rx" / name).read_bytes()))

    return telemetry, sender, reader, store, cross


class TestEgressEndToEnd:
    def test_every_component_rides_the_channel_without_bad_writes(self, tmp_path):
        telemetry, sender, reader, store, cross = egress_link(tmp_path)
        gate = BrokerGate()
        run = RunControl()
        broker = ActionBroker(ScriptedModel(), gate=gate)
        registry = SiteRegistry(tmp_path / "registry.json")
        tier = QuarantineTier(tmp_path / "logs", KEY, KNOWN_SOURCES, NullScanner())
        dog = Watchdog([CounterSource(DetectorId.D1_DENIED_FLOW, lambda: 0)],
                       ReferenceHaltActions(gate, run, tmp_path / "preserve"), liveness_timeout_s=10)
        for name, source in [("broker", broker.counters), ("run", run), ("registry", registry),
                             ("watchdog", dog), ("log_ingest", tier.counters)]:
            telemetry.attach(name, source)

        dog.poll(0)
        for t in range(1, 8):
            cross(t * 60)  # seven frames: enough for every rotating metric to appear once

        assert sender.counters.bad_writes == 0, "a component named a metric the registry lacks"
        assert reader.counters.frames_dropped == 0
        assert store.value("sys.heartbeat") == 1
        assert store.value("run.state") == 0
        assert store.value("sandbox.watchdog_state") == 0
        assert store.value("sites.live") == 0
        assert not store.go_to_the_terminal()

    def test_a_halt_reaches_the_dashboard_as_integers(self, tmp_path):
        telemetry, sender, reader, store, cross = egress_link(tmp_path)
        gate, run = BrokerGate(), RunControl()
        denied = {"n": 0}
        dog = Watchdog([CounterSource(DetectorId.D1_DENIED_FLOW, lambda: denied["n"])],
                       ReferenceHaltActions(gate, run, tmp_path / "preserve"), liveness_timeout_s=10)
        telemetry.attach("watchdog", dog)
        dog.poll(0)
        cross(60)
        assert not store.go_to_the_terminal()

        denied["n"] = 1
        dog.poll(1)
        cross(120)
        assert store.go_to_the_terminal()
        assert store.value("sandbox.escape_indicator") == 2
        assert store.value("sandbox.first_detector_id") == 1
        assert store.value("sandbox.watchdog_state") == 2
        lines = (tmp_path / "dev" / "readings.jsonl").read_text().splitlines()
        assert all(isinstance(json.loads(line)["value"], (int, float)) for line in lines)

    def test_unregistered_metric_name_is_refused_and_counted(self, tmp_path):
        telemetry, sender, *_ = egress_link(tmp_path)
        telemetry.attach("rogue", lambda: {"agent.secret_string_channel": 1, "run.state": 1})
        assert telemetry.collect() == 1
        assert sender.counters.bad_writes == 1

    def test_corruption_on_the_link_is_dropped_not_decoded(self, tmp_path):
        registry = load_registry()
        sender = EgressSender(registry, KEY)
        telemetry = Telemetry(sender)
        spool = FrameSpool(tmp_path / "tx")
        diode = FakeDemoDataDiode(tmp_path / "tx", tmp_path / "rx", Direction.EGRESS,
                                  max_item_bytes=EGRESS_FRAME_BYTES, corruption_rate=1.0, seed=1)
        reader = EgressReader(registry, KEY)
        spool.put(telemetry.tick(60))
        for name in diode.tick().delivered:
            assert reader.feed((tmp_path / "rx" / name).read_bytes()) == []
        assert reader.counters.frames_dropped == 1


@pytest.fixture
def factory(tmp_path):
    work = tmp_path / "c"
    shutil.copytree(EXAMPLE / "content", work / "content")
    shutil.copytree(EXAMPLE / "index", work / "index")
    spec = json.loads((EXAMPLE / "spec" / "site.json").read_text())
    return SiteEnvFactory(spec, work, work / "content" / "seed.sqlite", tmp_path / "eps")


class TestLogChannelEndToEnd:
    def test_trajectories_cross_the_log_diode_into_the_promoted_tier(self, tmp_path, factory):
        from inspect_ai import eval as inspect_eval

        emitter = LogEmitter(KEY, LogSource.EVAL_LOOP, tmp_path / "log-tx", clock=lambda: 1_700_000_000)
        question = EvalQuestion(id="q", task="t", site_id="site-000001", gold=GoldState(table="replies"), max_steps=3)
        log = inspect_eval(build_task([question], ActionBroker(ScriptedModel()), factory, emitter=emitter),
                           model="mockllm/model", log_dir=str(tmp_path / "inspect"), display="none", epochs=2)[0]
        assert log.status == "success", log.error

        diode = FakeDemoDataDiode(tmp_path / "log-tx", tmp_path / "log-rx", Direction.LOG)
        assert len(diode.drain().delivered) == 2
        tier = QuarantineTier(tmp_path / "logging", KEY, KNOWN_SOURCES, NullScanner())
        report = ingest_directory(tier, tmp_path / "log-rx")
        assert len(report.promoted) == 2 and report.rejected == []
        assert tier.counters.sequence_gaps == 0
        promoted = sorted((tmp_path / "logging" / "promoted" / "trajectory" / "1").glob("*.rec"))
        assert len(promoted) == 2
        payload = json.loads(promoted[0].read_bytes())
        assert payload["transcript"][0]["action"] == "stop"

    def test_a_record_that_never_arrives_is_a_visible_gap(self, tmp_path):
        emitter = LogEmitter(KEY, LogSource.WATCHDOG, tmp_path / "tx", clock=lambda: 1)
        paths = [emitter.emit(Stream.WATCHDOG, Severity.CRITICAL, b"halt %d" % i) for i in range(3)]
        paths[1].unlink()  # lost on the link, or deleted by something that wished it had not been written
        diode = FakeDemoDataDiode(tmp_path / "tx", tmp_path / "rx", Direction.LOG)
        diode.drain()
        tier = QuarantineTier(tmp_path / "logging", KEY, KNOWN_SOURCES, NullScanner())
        ingest_directory(tier, tmp_path / "rx")
        assert tier.counters.records_ingested == 2
        assert tier.counters.sequence_gaps == 1

    def test_unknown_source_is_rejected_at_ingest(self, tmp_path):
        from tools.log_ingest.frame import build_record

        record = build_record(KEY, 99, 1, 1, Stream.SYSTEM, Severity.INFO, b"hello")
        tier = QuarantineTier(tmp_path / "logging", KEY, KNOWN_SOURCES, NullScanner())
        report = tier.ingest(record)
        assert report.promoted == [] and tier.counters.records_ingested == 0

    def test_worker_and_watchdog_emit(self, tmp_path):
        from tools.command_executor import RunControl

        emitter = LogEmitter(KEY, LogSource.WATCHDOG, tmp_path / "tx", clock=lambda: 5)
        gate, run = BrokerGate(), RunControl()
        actions = ReferenceHaltActions(gate, run, tmp_path / "preserve", emitter=emitter)
        n = {"v": 0}
        dog = Watchdog([CounterSource(DetectorId.D3_CANARY_HOST, lambda: n["v"])], actions, liveness_timeout_s=10)
        dog.poll(0)
        n["v"] = 1
        dog.poll(1)
        assert emitter.sequence == 1
        tier = QuarantineTier(tmp_path / "logging", KEY, KNOWN_SOURCES, NullScanner())
        tier.ingest(next((tmp_path / "tx").glob("*.rec")).read_bytes())
        promoted = next((tmp_path / "logging" / "promoted" / "watchdog").rglob("*.rec"))
        assert json.loads(promoted.read_bytes())["first_detector"] == "D3_CANARY_HOST"
