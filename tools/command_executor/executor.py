from __future__ import annotations

import base64
import json
import shutil
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from nacl.signing import VerifyKey

from tools.receiver import Receiver
from tools.registry import SiteRegistry, SiteStatus

from .run_control import STARTABLE, RunControl, RunState

# Which bundle types a rotated-in key may sign, by the role the command declares.
ROLE_TYPES = {
    "pipeline": frozenset({"site", "index_only"}),
    "dev": frozenset({"command"}),
}

# Per-op parameter sets. The manifest schema bounds every value's shape; this
# bounds which ones each op may carry. Both are exact: a stray key is a reject.
OPS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    #                     required                              optional
    "start_run": (frozenset({"run_id", "config_ref"}), frozenset({"seed", "episodes", "sites"})),
    "stop_run": (frozenset({"run_id"}), frozenset()),
    "set_live": (frozenset({"bundle_id"}), frozenset()),
    "retire": (frozenset({"bundle_id"}), frozenset()),
    "set_sequence_floor": (frozenset({"signer_key_id", "sequence_floor"}), frozenset()),
    "rotate_verification_key": (
        frozenset({"new_signer_key_id", "new_verification_key_b64", "new_signer_role"}),
        frozenset(),
    ),
}
TOP_LEVEL = frozenset({"run_id", "config_ref"})


class CommandStatus(IntEnum):
    """schemas/status-codes.toml [command]."""

    APPLIED = 0
    REJECT_UNKNOWN_OP = 50
    REJECT_PARAMS = 51
    REJECT_SIGNER_ROLE = 52
    REJECT_TARGET = 53
    REJECT_STATE = 54


@dataclass(frozen=True)
class CommandReceipt:
    bundle_id: str
    op: str
    status: CommandStatus
    detail: str = field(default="", repr=False)  # inside log only; never crosses out


@dataclass
class ExecutorCounters:
    applied: int = 0
    rejected: int = 0
    last_code: int = 0


class CommandExecutor:
    """Applies command bundles from the receiver's command inbox, in sequence order."""

    def __init__(
        self,
        inbox: Path,
        receiver: Receiver,
        registry: SiteRegistry,
        run_control: RunControl,
        *,
        rotation_key_ids: frozenset[str],
    ) -> None:
        self.inbox = Path(inbox)
        self._receiver = receiver
        self._registry = registry
        self._run = run_control
        self._rotation_key_ids = rotation_key_ids
        self.counters = ExecutorCounters()
        self.receipts: list[CommandReceipt] = []

    def pending(self) -> list[str]:
        entries = []
        for path in self.inbox.iterdir():
            if path.is_dir() and (path / "manifest.json").is_file():
                entries.append((json.loads((path / "manifest.json").read_text())["sequence"], path.name))
        return [name for _, name in sorted(entries)]

    def run_once(self) -> CommandReceipt | None:
        queue = self.pending()
        return self.execute(queue[0]) if queue else None

    def execute(self, bundle_id: str) -> CommandReceipt:
        source = self.inbox / bundle_id
        manifest = json.loads((source / "manifest.json").read_text())
        assert manifest["type"] == "command", "a non-command reached the command inbox"
        try:
            receipt = self._apply(bundle_id, manifest)
        finally:
            shutil.rmtree(source, ignore_errors=True)  # applied or refused, never re-read
        self.receipts.append(receipt)
        self.counters.last_code = int(receipt.status)
        if receipt.status is CommandStatus.APPLIED:
            self.counters.applied += 1
        else:
            self.counters.rejected += 1
        return receipt

    # -- validation --------------------------------------------------------

    def _apply(self, bundle_id: str, manifest: dict) -> CommandReceipt:
        command = manifest["command"]
        op = command["op"]
        if op not in OPS:
            return CommandReceipt(bundle_id, op, CommandStatus.REJECT_UNKNOWN_OP)

        required, optional = OPS[op]
        given = {k: v for k, v in command.items() if k != "op" and k in TOP_LEVEL}
        given.update(command.get("params") or {})
        if not required <= set(given) or not set(given) <= required | optional:
            return CommandReceipt(bundle_id, op, CommandStatus.REJECT_PARAMS, f"keys {sorted(given)}")

        handler = getattr(self, f"_op_{op}")
        return handler(bundle_id, manifest["signer_key_id"], given)

    # -- ops -----------------------------------------------------------------

    def _op_start_run(self, bundle_id: str, signer: str, p: dict) -> CommandReceipt:
        op = "start_run"
        for site_id in p.get("sites", []):
            if not any(r.site_id == site_id for r in self._registry.live_sites()):
                return CommandReceipt(bundle_id, op, CommandStatus.REJECT_TARGET, f"no live {site_id}")
        if self._run.halted or self._run.state not in STARTABLE:
            return CommandReceipt(bundle_id, op, CommandStatus.REJECT_STATE, self._run.state.name)
        self._run.start(p["run_id"], p["config_ref"], {k: p[k] for k in ("seed", "episodes", "sites") if k in p})
        return CommandReceipt(bundle_id, op, CommandStatus.APPLIED)

    def _op_stop_run(self, bundle_id: str, signer: str, p: dict) -> CommandReceipt:
        op = "stop_run"
        if p["run_id"] != self._run.run_id:
            return CommandReceipt(bundle_id, op, CommandStatus.REJECT_TARGET, "not the current run")
        if self._run.state not in (RunState.RUNNING, RunState.PAUSED):
            return CommandReceipt(bundle_id, op, CommandStatus.REJECT_STATE, self._run.state.name)
        self._run.stop()
        return CommandReceipt(bundle_id, op, CommandStatus.APPLIED)

    def _op_set_live(self, bundle_id: str, signer: str, p: dict) -> CommandReceipt:
        op = "set_live"
        record = self._registry.get(p["bundle_id"])
        if record is None:
            return CommandReceipt(bundle_id, op, CommandStatus.REJECT_TARGET)
        if record.status is not SiteStatus.RETIRED:
            return CommandReceipt(bundle_id, op, CommandStatus.REJECT_STATE, record.status.name)
        self._registry.set_live(p["bundle_id"])
        return CommandReceipt(bundle_id, op, CommandStatus.APPLIED)

    def _op_retire(self, bundle_id: str, signer: str, p: dict) -> CommandReceipt:
        op = "retire"
        record = self._registry.get(p["bundle_id"])
        if record is None:
            return CommandReceipt(bundle_id, op, CommandStatus.REJECT_TARGET)
        if record.status is not SiteStatus.LIVE:
            return CommandReceipt(bundle_id, op, CommandStatus.REJECT_STATE, record.status.name)
        self._registry.retire(p["bundle_id"])
        return CommandReceipt(bundle_id, op, CommandStatus.APPLIED)

    def _op_set_sequence_floor(self, bundle_id: str, signer: str, p: dict) -> CommandReceipt:
        op = "set_sequence_floor"
        if p["signer_key_id"] not in self._receiver.verify_keys:
            return CommandReceipt(bundle_id, op, CommandStatus.REJECT_TARGET, "unknown signer")
        self._receiver.set_sequence_floor(p["signer_key_id"], p["sequence_floor"])
        return CommandReceipt(bundle_id, op, CommandStatus.APPLIED)

    def _op_rotate_verification_key(self, bundle_id: str, signer: str, p: dict) -> CommandReceipt:
        op = "rotate_verification_key"
        if signer not in self._rotation_key_ids:
            return CommandReceipt(bundle_id, op, CommandStatus.REJECT_SIGNER_ROLE, signer)
        if p["new_signer_key_id"] in self._receiver.verify_keys:
            return CommandReceipt(bundle_id, op, CommandStatus.REJECT_STATE, "key id already trusted")
        raw = base64.b64decode(p["new_verification_key_b64"], validate=True)
        if len(raw) != 32:
            return CommandReceipt(bundle_id, op, CommandStatus.REJECT_PARAMS, "not an Ed25519 public key")
        self._receiver.add_verify_key(p["new_signer_key_id"], VerifyKey(raw), ROLE_TYPES[p["new_signer_role"]])
        return CommandReceipt(bundle_id, op, CommandStatus.APPLIED)
