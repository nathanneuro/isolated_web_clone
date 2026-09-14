"""Run the whole pipeline on one machine, both sides of a simulated diode.

    uv run python scripts/run_demo.py

Exercises every component except the hardware: bundle-build outside, the
fake_demo_data_diode in place of a one-way link, the receiver and go-live inside.
Writes to a timestamped run directory under outputs/.

What this demonstrates, and what it does not: the protocol, the crypto, and the
fail-closed paths are real. The isolation is not -- it is one Python process on one
host. See tools/fake_demo_data_diode/__init__.py.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nacl.signing import VerifyKey  # noqa: E402

from tools.bundle_build import build_bundle  # noqa: E402
from tools.bundle_build.keys import KeyRole, generate_demo_keyset, load_signing_identity  # noqa: E402
from tools.fake_demo_data_diode import DEMO_BANNER, Direction, FakeDemoDataDiode  # noqa: E402
from tools.golive import GoLiveService  # noqa: E402
from tools.receiver import Receiver, Status  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "example" / "synthetic_site"
RESULT_NAMES = {0: "pass", 1: "FAIL", 2: "skip", 3: "ERROR"}


def step(number: int, side: str, title: str) -> None:
    print(f"\n[{number}] {side:<7} {title}")


def main() -> int:
    print(DEMO_BANNER)
    run_dir = ROOT / "outputs" / f"run_{datetime.now():%Y%m%d_%H%M%S}_demo_pipeline"
    (run_dir / "plots").mkdir(parents=True)
    keys = run_dir / "keys"
    generate_demo_keyset(keys)

    if not (EXAMPLE / "content" / "seed.sqlite").exists():
        print("generating example content first...")
        sys.path.insert(0, str(EXAMPLE))
        import generate_content

        generate_content.main()

    step(1, "OUTSIDE", "bundle-build: encrypt, rewrite handles, lint, sign")
    identity = load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)
    archive = build_bundle(
        EXAMPLE, run_dir / "outbox",
        identity=identity,
        golive_public_key_path=keys / "golive-wrapping.pub",
        sequence=1,
    )
    manifest = json.loads((archive.parent / archive.name.removesuffix(".tar") / "manifest.json").read_text())
    print(f"    {archive.name}  {archive.stat().st_size:,} B  {len(manifest['files'])} files")
    print(f"    content key sealed to {manifest['content_key_wrapped']['recipient_key_id']}")

    step(2, "DIODE", "fake_demo_data_diode ingress (SIMULATED)")
    transmit = run_dir / "diode-transmit"
    transmit.mkdir()
    shutil.move(str(archive), transmit / archive.name)
    record = FakeDemoDataDiode(transmit, run_dir / "inbox", Direction.INGRESS).tick()
    print(f"    delivered {record.delivered}, dropped {record.dropped}")

    step(3, "INSIDE", "receiver: layout, signature, sequence, hashes, lint")
    receiver = Receiver(
        run_dir / "state", run_dir / "worker-inbox", run_dir / "command-inbox", run_dir / "quarantine",
        {"pipeline-demo": (VerifyKey((keys / "pipeline-verify.pub").read_bytes()), frozenset({"site", "index_only"}))},
    )
    receipt = receiver.receive(run_dir / "inbox" / archive.name)
    print(f"    status {int(receipt.status)} ({receipt.status.name})  bundle {receipt.bundle_id}")
    if not receipt.accepted:
        print(f"    quarantined. inside-only detail: {receipt.detail}")
        return 1

    step(4, "INSIDE", "go-live: unwrap key, decrypt into sandbox, compose, run suite")
    result = GoLiveService(keys / "golive-wrapping.key", run_dir / "sandboxes").go_live(
        run_dir / "worker-inbox" / receipt.bundle_id
    )
    print(f"    status {int(result.status)} ({result.status.name})")
    for test in result.tests:
        print(f"      {test.test_id}  {RESULT_NAMES[test.code]}")

    step(5, "INSIDE", "what crosses back to the worker")
    print(f"    {json.dumps(result.for_worker())[:120]}...")
    print("    integers and test IDs only: no output, no diffs, no decrypted anything")

    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "run": run_dir.name,
                "bundle_id": receipt.bundle_id,
                "receiver_status": int(receipt.status),
                "golive_status": int(result.status),
                "tests": {t.test_id: t.code for t in result.tests},
                "diode": "fake_demo_data_diode (SIMULATED, provides no isolation)",
            },
            indent=1,
        )
    )
    print(f"\nrun directory: {run_dir.relative_to(ROOT)}")
    return 0 if result.status == 30 else 1


if __name__ == "__main__":
    sys.exit(main())
