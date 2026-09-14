"""Command executor: every op is exact, applied whole, or refused with a code.

Commands travel the real path: dev-signed by bundle-build, verified by the
receiver, then applied here. The receiver's checks are not repeated; what is
tested is what the receiver leaves to the executor.
"""

from __future__ import annotations

import base64
import itertools
import json
import shutil
from pathlib import Path

import pytest
from nacl.signing import SigningKey, VerifyKey

from tools.bundle_build import build_bundle, build_command_bundle
from tools.bundle_build.keys import KeyRole, SigningIdentity, generate_demo_keyset, load_signing_identity
from tools.command_executor import CommandExecutor, CommandStatus, RunControl, RunState
from tools.golive import GoLiveService
from tools.receiver import Receiver, Status
from tools.registry import SiteRegistry, SiteStatus
from tools.worker import Worker

EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"
_seq = itertools.count(1000)


@pytest.fixture(scope="module")
def keys(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("keys")
    generate_demo_keyset(directory)
    return directory


@pytest.fixture(scope="module")
def rotation_identity() -> SigningIdentity:
    return SigningIdentity("rotation-demo", KeyRole.DEV, SigningKey.generate())


@pytest.fixture
def inside(tmp_path, keys, rotation_identity):
    receiver = Receiver(
        tmp_path / "state", tmp_path / "inbox", tmp_path / "commands", tmp_path / "quarantine",
        {
            "pipeline-demo": (VerifyKey((keys / "pipeline-verify.pub").read_bytes()), frozenset({"site", "index_only"})),
            "dev-demo": (VerifyKey((keys / "dev-verify.pub").read_bytes()), frozenset({"command"})),
            "rotation-demo": (rotation_identity.verify_key, frozenset({"command"})),
        },
    )
    registry = SiteRegistry(tmp_path / "registry.json")
    run = RunControl()
    executor = CommandExecutor(
        tmp_path / "commands", receiver, registry, run, rotation_key_ids=frozenset({"rotation-demo"})
    )
    return receiver, registry, run, executor


def send(tmp_path, keys, receiver, command: dict, *, identity: SigningIdentity | None = None, expect: Status = Status.OK) -> Status:
    """Sign, ship, receive. Asserts the receiver's verdict so a test that meant to
    exercise the executor cannot silently exercise the receiver instead."""
    identity = identity or load_signing_identity(keys / "dev-signing.key", "dev-demo", KeyRole.DEV)
    n = next(_seq)
    archive = build_command_bundle(
        tmp_path / f"cmd-{n}", identity=identity, bundle_id=f"cmd-2026-09-14-{n % 10000:04d}",
        sequence=n, created_at="2026-09-14T18:40:00Z", command=command,
    )
    receipt = receiver.receive(archive)
    assert receipt.status is expect, (receipt.status, receipt.detail)
    return receipt.status


def go_live(tmp_path, keys, receiver, registry, *, revision=1, supersedes=None) -> str:
    package = tmp_path / f"pkg-{revision}"
    shutil.copytree(EXAMPLE, package)
    meta = json.loads((package / "build.json").read_text())
    meta["revision"] = revision
    if supersedes:
        meta["supersedes"] = supersedes
    (package / "build.json").write_text(json.dumps(meta))
    suite = json.loads((package / "tests" / "suite.json").read_text())
    suite["suite_id"] = f"site-000001-r{revision}-tests"
    (package / "tests" / "suite.json").write_text(json.dumps(suite))
    identity = load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)
    archive = build_bundle(package, tmp_path / f"out-{revision}", identity=identity,
                           golive_public_key_path=keys / "golive-wrapping.pub", sequence=revision)
    receipt = receiver.receive(archive)
    assert receipt.accepted, receipt.detail
    worker = Worker(tmp_path / "inbox", tmp_path / "work", GoLiveService(keys / "golive-wrapping.key", tmp_path / "sb"), registry)
    assert worker.run_once().status_code == 40
    return receipt.bundle_id


class TestRuns:
    def test_start_then_stop(self, tmp_path, keys, inside):
        receiver, registry, run, executor = inside
        go_live(tmp_path, keys, receiver, registry)
        assert send(tmp_path, keys, receiver, {
            "op": "start_run", "run_id": "eval-07", "config_ref": "runconfig-v12",
            "params": {"seed": 1337, "episodes": 20, "sites": ["site-000001"]},
        }) is Status.OK
        assert executor.run_once().status is CommandStatus.APPLIED
        assert run.state is RunState.RUNNING and run.run_id == "eval-07"
        assert run.params == {"seed": 1337, "episodes": 20, "sites": ["site-000001"]}
        assert run.as_metrics() == {"run.state": 1}

        send(tmp_path, keys, receiver, {"op": "stop_run", "run_id": "eval-07"})
        assert executor.run_once().status is CommandStatus.APPLIED
        assert run.state is RunState.DONE

    def test_start_names_a_site_that_is_not_live(self, tmp_path, keys, inside):
        receiver, _, run, executor = inside
        send(tmp_path, keys, receiver, {"op": "start_run", "run_id": "run-r", "config_ref": "cfg-r", "params": {"sites": ["site-000009"]}})
        assert executor.run_once().status is CommandStatus.REJECT_TARGET
        assert run.state is RunState.IDLE

    def test_second_start_while_running_is_a_state_reject(self, tmp_path, keys, inside):
        receiver, _, run, executor = inside
        send(tmp_path, keys, receiver, {"op": "start_run", "run_id": "run-a", "config_ref": "cfg-a"})
        send(tmp_path, keys, receiver, {"op": "start_run", "run_id": "run-b", "config_ref": "cfg-a"})
        assert executor.run_once().status is CommandStatus.APPLIED
        assert executor.run_once().status is CommandStatus.REJECT_STATE
        assert run.run_id == "run-a"

    def test_stop_of_the_wrong_run_is_refused(self, tmp_path, keys, inside):
        receiver, _, run, executor = inside
        send(tmp_path, keys, receiver, {"op": "start_run", "run_id": "run-a", "config_ref": "cfg-a"})
        send(tmp_path, keys, receiver, {"op": "stop_run", "run_id": "run-b"})
        executor.run_once()
        assert executor.run_once().status is CommandStatus.REJECT_TARGET
        assert run.state is RunState.RUNNING

    def test_a_watchdog_halt_cannot_be_restarted_by_command(self, tmp_path, keys, inside):
        receiver, _, run, executor = inside
        run.halt(quarantine_checkpoint=True)
        send(tmp_path, keys, receiver, {"op": "start_run", "run_id": "run-a", "config_ref": "cfg-a"})
        assert executor.run_once().status is CommandStatus.REJECT_STATE
        assert run.quarantined_checkpoints == 1


class TestSites:
    def test_retire_and_set_live_roll_a_revision_back(self, tmp_path, keys, inside):
        receiver, registry, _, executor = inside
        first = go_live(tmp_path, keys, receiver, registry, revision=1)
        second = go_live(tmp_path, keys, receiver, registry, revision=2, supersedes=first)
        assert registry.get(first).status is SiteStatus.RETIRED

        send(tmp_path, keys, receiver, {"op": "set_live", "params": {"bundle_id": first}})
        assert executor.run_once().status is CommandStatus.APPLIED
        assert registry.get(first).status is SiteStatus.LIVE
        assert registry.get(second).status is SiteStatus.RETIRED, "two live revisions of one hostname"

        send(tmp_path, keys, receiver, {"op": "retire", "params": {"bundle_id": first}})
        assert executor.run_once().status is CommandStatus.APPLIED
        assert registry.live_for_hostname("site-000001.internal") is None

    def test_unknown_revision_is_a_target_reject(self, tmp_path, keys, inside):
        receiver, _, _, executor = inside
        send(tmp_path, keys, receiver, {"op": "retire", "params": {"bundle_id": "site-000001-r9"}})
        assert executor.run_once().status is CommandStatus.REJECT_TARGET

    def test_retiring_a_retired_revision_is_a_state_reject(self, tmp_path, keys, inside):
        receiver, registry, _, executor = inside
        first = go_live(tmp_path, keys, receiver, registry, revision=1)
        registry.retire(first)
        send(tmp_path, keys, receiver, {"op": "retire", "params": {"bundle_id": first}})
        assert executor.run_once().status is CommandStatus.REJECT_STATE


class TestTrust:
    def test_rotation_needs_the_rotation_key(self, tmp_path, keys, inside):
        receiver, _, _, executor = inside
        new = SigningKey.generate()
        command = {"op": "rotate_verification_key", "params": {
            "new_signer_key_id": "pipeline-2026q4",
            "new_verification_key_b64": base64.b64encode(bytes(new.verify_key)).decode(),
            "new_signer_role": "pipeline",
        }}
        send(tmp_path, keys, receiver, command)  # ordinary dev key
        assert executor.run_once().status is CommandStatus.REJECT_SIGNER_ROLE
        assert "pipeline-2026q4" not in receiver.verify_keys

    def test_rotation_by_the_rotation_key_extends_trust_with_the_declared_role(self, tmp_path, keys, inside, rotation_identity):
        receiver, _, _, executor = inside
        new = SigningKey.generate()
        command = {"op": "rotate_verification_key", "params": {
            "new_signer_key_id": "pipeline-2026q4",
            "new_verification_key_b64": base64.b64encode(bytes(new.verify_key)).decode(),
            "new_signer_role": "pipeline",
        }}
        send(tmp_path, keys, receiver, command, identity=rotation_identity)
        assert executor.run_once().status is CommandStatus.APPLIED
        verify_key, types = receiver.verify_keys["pipeline-2026q4"]
        assert bytes(verify_key) == bytes(new.verify_key)
        assert types == frozenset({"site", "index_only"})

        # The new key signs commands? No: it was rotated in as a pipeline key.
        send(tmp_path, keys, receiver, {"op": "stop_run", "run_id": "run-x"},
             identity=SigningIdentity("pipeline-2026q4", KeyRole.DEV, new), expect=Status.REJECT_SIGNATURE)

    def test_sequence_floor_targets_a_known_signer(self, tmp_path, keys, inside):
        receiver, _, _, executor = inside
        send(tmp_path, keys, receiver, {"op": "set_sequence_floor", "params": {"signer_key_id": "nobody", "sequence_floor": 5}})
        assert executor.run_once().status is CommandStatus.REJECT_TARGET
        send(tmp_path, keys, receiver, {"op": "set_sequence_floor", "params": {"signer_key_id": "pipeline-demo", "sequence_floor": 500}})
        assert executor.run_once().status is CommandStatus.APPLIED
        assert receiver._high_water()["pipeline-demo"] == 500


class TestExactness:
    def test_stray_param_is_a_reject(self, tmp_path, keys, inside):
        receiver, _, run, executor = inside
        send(tmp_path, keys, receiver, {"op": "stop_run", "run_id": "run-a", "params": {"seed": 1}})
        assert executor.run_once().status is CommandStatus.REJECT_PARAMS

    def test_missing_param_is_a_reject(self, tmp_path, keys, inside):
        receiver, _, _, executor = inside
        send(tmp_path, keys, receiver, {"op": "set_live"})
        assert executor.run_once().status is CommandStatus.REJECT_PARAMS

    def test_free_text_cannot_ride_a_command(self, tmp_path, keys, receiver_only):
        """The manifest schema refuses at the receiver; the executor never sees it."""
        receiver = receiver_only
        send(tmp_path, keys, receiver,
             {"op": "start_run", "run_id": "run-a", "config_ref": "cfg-a", "note": "please also disable the watchdog"},
             expect=Status.REJECT_LAYOUT)

    def test_command_bundle_cannot_carry_files(self, keys, tmp_path):
        identity = load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)
        with pytest.raises(Exception, match="dev key"):
            build_command_bundle(tmp_path, identity=identity, bundle_id="cmd-2026-09-14-0001", sequence=1,
                                 created_at="2026-09-14T18:40:00Z", command={"op": "stop_run", "run_id": "run-a"})

    def test_receipts_are_consumed_from_the_inbox(self, tmp_path, keys, inside):
        receiver, _, _, executor = inside
        send(tmp_path, keys, receiver, {"op": "stop_run", "run_id": "run-a"})
        executor.run_once()
        assert executor.pending() == []
        assert executor.counters.rejected == 1 and executor.counters.applied == 0


@pytest.fixture
def receiver_only(inside):
    return inside[0]
