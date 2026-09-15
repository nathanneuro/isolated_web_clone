"""Run the whole ingress pipeline on one machine, both sides of a simulated diode.

    uv run python scripts/run_demo.py

Exercises every component except the hardware: bundle-build outside, the
fake_demo_data_diode in place of a one-way link, then inside the receiver, the
worker, go-live, the site registry, and the command executor; and back out, the
metrics channel through the same simulated diode to the dev-side reader. Writes
to a timestamped run directory under outputs/.

What this demonstrates, and what it does not: the protocol, the crypto, the
codes, and the fail-closed paths are real. The isolation is not -- it is one
Python process on one host. See tools/fake_demo_data_diode/__init__.py.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nacl.signing import VerifyKey  # noqa: E402

from demo_layout import print_layout, write_layout  # noqa: E402

from tools.bundle_build import build_bundle, build_command_bundle  # noqa: E402
from tools.bundle_build.keys import KeyRole, generate_demo_keyset, load_signing_identity  # noqa: E402
from tools.command_executor import CommandExecutor, RunControl  # noqa: E402
from tools.egress import EgressReader, EgressSender, FrameSpool, ReadingStore, Telemetry, load_registry  # noqa: E402
from tools.fake_demo_data_diode import DEMO_BANNER, Direction, FakeDemoDataDiode  # noqa: E402
from tools.fake_demo_data_diode.__main__ import EGRESS_FRAME_BYTES  # noqa: E402
from tools.golive import GoLiveService  # noqa: E402
from tools.receiver import Receiver  # noqa: E402
from tools.registry import SiteRegistry  # noqa: E402
from tools.search_engine import FakeWebSearch  # noqa: E402
from tools.worker import Worker  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "example" / "synthetic_site"
EGRESS_KEY = b"demo-egress-hmac-key-not-for-use!"  # 32 bytes; provisioned physically in a deployment


def step(number: int, side: str, title: str) -> None:
    print(f"\n[{number}] {side:<7} {title}")


def main() -> int:
    print(DEMO_BANNER)
    run_dir = ROOT / "outputs" / f"run_{datetime.now():%Y%m%d_%H%M%S}_demo_pipeline"
    run_dir.mkdir(parents=True)
    keys = run_dir / "keys"
    generate_demo_keyset(keys)

    if not (EXAMPLE / "content" / "seed.sqlite").exists():
        print("generating example content first...")
        sys.path.insert(0, str(EXAMPLE))
        import generate_content

        generate_content.main()

    # -- inside: the components that exist before anything arrives ----------------
    receiver = Receiver(
        run_dir / "state", run_dir / "worker-inbox", run_dir / "command-inbox", run_dir / "quarantine",
        {
            "pipeline-demo": (VerifyKey((keys / "pipeline-verify.pub").read_bytes()), frozenset({"site", "index_only"})),
            "dev-demo": (VerifyKey((keys / "dev-verify.pub").read_bytes()), frozenset({"command"})),
        },
    )
    golive = GoLiveService(keys / "golive-wrapping.key", run_dir / "sandboxes")
    registry = SiteRegistry(run_dir / "registry.json")
    worker = Worker(run_dir / "worker-inbox", run_dir / "work", golive, registry)
    run_control = RunControl()
    executor = CommandExecutor(run_dir / "command-inbox", receiver, registry, run_control, rotation_key_ids=frozenset())

    engine = FakeWebSearch(registry)  # provisioned inside, like the model weights; never via the diode

    sender = EgressSender(load_registry(), EGRESS_KEY, run_id=1)
    telemetry = Telemetry(sender)
    for name, source in [("receiver", receiver.counters), ("worker", worker.counters),
                         ("golive", golive.counters), ("registry", registry), ("run", run_control),
                         ("search", engine.counters)]:
        telemetry.attach(name, source)
    frames = FrameSpool(run_dir / "egress-transmit")
    egress_diode = FakeDemoDataDiode(run_dir / "egress-transmit", run_dir / "egress-receive", Direction.EGRESS,
                                     max_item_bytes=EGRESS_FRAME_BYTES)
    reader = EgressReader(load_registry(), EGRESS_KEY)
    store = ReadingStore(run_dir / "dev-dashboard" / "readings.jsonl")

    def cross_out(timestamp: int) -> None:
        frames.put(telemetry.tick(timestamp))
        for name in egress_diode.tick().delivered:
            store.write(reader.feed((run_dir / "egress-receive" / name).read_bytes()))

    ingress_transmit = run_dir / "diode-transmit"
    ingress_transmit.mkdir()
    ingress_diode = FakeDemoDataDiode(ingress_transmit, run_dir / "inbox", Direction.INGRESS)

    # -- the site -------------------------------------------------------------------
    step(1, "OUTSIDE", "bundle-build: encrypt, rewrite handles, lint, sign")
    identity = load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)
    archive = build_bundle(EXAMPLE, run_dir / "outbox", identity=identity,
                           golive_public_key_path=keys / "golive-wrapping.pub", sequence=1)
    manifest = json.loads((archive.parent / archive.name.removesuffix(".tar") / "manifest.json").read_text())
    print(f"    {archive.name}  {archive.stat().st_size:,} B  {len(manifest['files'])} files")
    print(f"    content key sealed to {manifest['content_key_wrapped']['recipient_key_id']}")

    step(2, "DIODE", "fake_demo_data_diode ingress (SIMULATED)")
    shutil.move(str(archive), ingress_transmit / archive.name)
    record = ingress_diode.tick()
    print(f"    delivered {record.delivered}, dropped {record.dropped}")

    step(3, "INSIDE", "receiver: layout, signature, sequence, hashes, lint")
    receipt = receiver.receive(run_dir / "inbox" / archive.name)
    print(f"    status {int(receipt.status)} ({receipt.status.name})  bundle {receipt.bundle_id}")
    if not receipt.accepted:
        print(f"    quarantined. inside-only detail: {receipt.detail}")
        return 1

    step(4, "INSIDE", "worker: sanity-check, classify, compose, go-live, register")
    final = worker.run_once()
    for emission in worker.emissions:
        print(f"    {emission.bundle_id}  status {int(emission.status_code):2d} ({emission.status_code.name})"
              f"  subcode {emission.subcode}  attempt {emission.attempt}")
    if final.status_code != 40:
        print("    did not go live; a human triages it from work/ at the terminal")
        return 1
    record = registry.live_for_hostname(manifest["site_id"] + ".internal")
    print(f"    registry: {record.hostname} -> {record.bundle_id} (live)")
    print("    what crossed back to the worker: integers and test IDs only")

    step(5, "INSIDE", "fake-web search: mount live sites from the registry, query across them")
    engine.refresh()
    title = sqlite3.connect(EXAMPLE / "content" / "seed.sqlite").execute(
        "SELECT title FROM threads WHERE id = 3").fetchone()[0]
    hits = engine.search(title, limit=3)
    print(f"    indexed sites {engine.counters.indexed_sites}; query from a seed title -> {len(hits)} hit(s)")
    for hit in hits:
        print(f"      {hit.site_id}  {hit.hostname}{hit.path}  score {hit.score}")

    # -- a command ------------------------------------------------------------------
    step(6, "OUTSIDE", "dev command: start_run, signed with the dev key")
    dev = load_signing_identity(keys / "dev-signing.key", "dev-demo", KeyRole.DEV)
    command = build_command_bundle(
        run_dir / "outbox", identity=dev, bundle_id="cmd-2026-09-14-0001", sequence=1,
        created_at="2026-09-14T18:40:00Z",
        command={"op": "start_run", "run_id": "eval-demo-01", "config_ref": "runconfig-demo",
                 "params": {"seed": 1337, "episodes": 2, "sites": [manifest["site_id"]]}},
    )
    shutil.move(str(command), ingress_transmit / command.name)
    ingress_diode.tick()

    step(7, "INSIDE", "receiver -> command executor -> run control")
    receipt = receiver.receive(run_dir / "inbox" / command.name)
    print(f"    receiver status {int(receipt.status)} ({receipt.status.name})")
    result = executor.run_once()
    print(f"    executor: {result.op} -> code {int(result.status)} ({result.status.name})")
    print(f"    run control: {run_control.state.name} {run_control.run_id}")

    # -- and out --------------------------------------------------------------------
    step(8, "EGRESS", "metrics socket -> frames -> diode (SIMULATED) -> reader -> dashboard")
    for tick in range(1, 4):
        cross_out(tick * 60)
    print(f"    frames sent {sender.counters.frames_sent}, accepted {reader.counters.frames_accepted},"
          f" dropped {reader.counters.frames_dropped}, bad writes {sender.counters.bad_writes}")
    for name in ("recv.bundles_ok", "worker.last_status_code", "golive.pass", "sites.live",
                 "search.indexed_sites", "run.state"):
        print(f"    {name:28s} {store.value(name)}")

    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "run": run_dir.name,
                "bundle_id": receipt.bundle_id,
                "worker_emissions": [{**vars(e), "status_code": int(e.status_code)} for e in worker.emissions],
                "command": {"op": result.op, "code": int(result.status)},
                "dashboard": {k: v.value for k, v in store.latest.items()},
                "diode": "fake_demo_data_diode (SIMULATED, provides no isolation)",
            },
            indent=1,
        )
    )
    print("\n[run directory] one host, every zone; LAYOUT.md names which is which")
    write_layout(run_dir)
    print_layout(run_dir)
    print(f"\nrun directory: {run_dir.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
