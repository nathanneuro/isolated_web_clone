"""Verification chain for arriving bundles (bundle-format-spec §8.2).

    1. verify archive layout
    2. verify manifest.sig against the pinned verification key
    3. check sequence against the persisted high-water mark
    4. verify every file hash and length
    5. run bundle-lint
    6. dispatch on `type`, and on nothing else
    7. emit a receipt code

Two rules govern every line here:

**A bundle that fails any step is quarantined, never partially processed.** There is
no "mostly valid" outcome and no repair. The receiver cannot ask the sender anything,
so a partial result is a thing nobody can resolve.

**Unsigned bytes are data.** The only file read before the signature is checked is
manifest.json, and it is read as JSON into a schema-validated shape, never
interpreted. The command parser runs only for `type: "command"` on a bundle whose
signature already verified (§4.6, §9).

The receiver holds only verification keys. Signing keys never enter the airgap.
"""

from __future__ import annotations

import json
import tarfile
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

import jsonschema
from blake3 import blake3
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from tools.bundle_lint import lint_bundle
from tools.bundle_lint.findings import SCHEMA_DIR, Finding

ACCEPTED_FORMAT_VERSIONS = frozenset({"0.1"})

# §3: the receiver rejects any archive containing paths outside this layout.
ALLOWED_TOP_LEVEL = frozenset({"manifest.json", "manifest.sig", "spec", "tests", "content", "index"})
MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_MEMBERS = 100_000


class Status(IntEnum):
    """schemas/status-codes.toml. These integers are what reach the dashboard."""

    OK = 0
    REJECT_LAYOUT = 10
    REJECT_SIGNATURE = 11
    REJECT_REPLAY = 12
    REJECT_HASH_MISMATCH = 13
    REJECT_LINT = 14


@dataclass(frozen=True)
class Receipt:
    """What the receiver emits. A code, and codes for the findings. No strings.

    `detail` exists for the inside log read at the wired terminal. It is never put
    on the egress channel and never returned to the worker.
    """

    status: Status
    bundle_id: str | None = None
    lint_codes: tuple[str, ...] = ()
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.status is Status.OK


@dataclass
class ReceiverCounters:
    """metrics-registry block 10-13. Integers; the receipt's detail never leaves."""

    bundles_ok: int = 0
    bundles_rejected: int = 0
    last_reject_code: int = 0
    sequence_high_water: int = 0

    def as_metrics(self) -> dict[str, int]:
        return {f"recv.{k}": v for k, v in vars(self).items()}


class Receiver:
    """Verifies arriving bundles and dispatches the ones that survive.

    `verify_keys` maps signer_key_id -> (VerifyKey, allowed bundle types). The type
    restriction is §9's rule that a site bundle signed with a dev key is rejected and
    vice versa, enforced as data rather than as a special case.
    """

    def __init__(
        self,
        state_dir: Path,
        worker_inbox: Path,
        command_inbox: Path,
        quarantine: Path,
        verify_keys: dict[str, tuple[VerifyKey, frozenset[str]]],
    ) -> None:
        self.state_dir = Path(state_dir)
        self.worker_inbox = Path(worker_inbox)
        self.command_inbox = Path(command_inbox)
        self.quarantine = Path(quarantine)
        self.verify_keys = verify_keys
        for directory in (self.state_dir, self.worker_inbox, self.command_inbox, self.quarantine):
            directory.mkdir(parents=True, exist_ok=True)
        self._sequence_file = self.state_dir / "sequence-high-water.json"
        self._trust_file = self.state_dir / "rotated-verify-keys.json"
        self.counters = ReceiverCounters()
        # Keys rotated in by command (§9) outlive the process. They are loaded after
        # the provisioned set so a provisioned key can never be silently replaced.
        if self._trust_file.exists():
            for key_id, entry in json.loads(self._trust_file.read_text()).items():
                assert key_id not in self.verify_keys, f"rotated key {key_id} collides with a provisioned key"
                self.verify_keys[key_id] = (VerifyKey(bytes.fromhex(entry["hex"])), frozenset(entry["types"]))

    def add_verify_key(self, key_id: str, key: VerifyKey, types: frozenset[str]) -> None:
        """§9 rotate_verification_key: extend trust, durably. Command-driven only."""
        assert key_id not in self.verify_keys, key_id
        self.verify_keys[key_id] = (key, types)
        rotated = json.loads(self._trust_file.read_text()) if self._trust_file.exists() else {}
        rotated[key_id] = {"hex": bytes(key).hex(), "types": sorted(types)}
        tmp = self._trust_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(rotated, indent=1))
        tmp.replace(self._trust_file)

    # -- sequence high-water mark (§4.2) ------------------------------------

    def _high_water(self) -> dict[str, int]:
        if not self._sequence_file.exists():
            return {}
        return json.loads(self._sequence_file.read_text())

    def _record_sequence(self, signer_key_id: str, sequence: int) -> None:
        marks = self._high_water()
        marks[signer_key_id] = sequence
        tmp = self._sequence_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(marks, sort_keys=True))
        tmp.replace(self._sequence_file)

    def set_sequence_floor(self, signer_key_id: str, floor: int) -> None:
        """§11.5: recovery when the persisted mark is lost. Command-driven only."""
        self._record_sequence(signer_key_id, floor)

    # -- the chain ----------------------------------------------------------

    def receive(self, archive_path: Path) -> Receipt:
        archive_path = Path(archive_path)
        unpacked = self.state_dir / "unpack" / archive_path.name
        if unpacked.exists():
            _rmtree(unpacked)

        layout = self._unpack(archive_path, unpacked)
        if layout is not None:
            return self._count(self._quarantine(archive_path, layout))

        receipt = self._verify(unpacked)
        if not receipt.accepted:
            _rmtree(unpacked)
            return self._count(self._quarantine(archive_path, receipt))

        self._dispatch(unpacked)
        return self._count(receipt)

    def _count(self, receipt: Receipt) -> Receipt:
        if receipt.accepted:
            self.counters.bundles_ok += 1
            self.counters.sequence_high_water = max(self._high_water().values(), default=0)
        else:
            self.counters.bundles_rejected += 1
            self.counters.last_reject_code = int(receipt.status)
        return receipt

    def _unpack(self, archive_path: Path, target: Path) -> Receipt | None:
        """Step 1. Returns a rejection receipt, or None if the layout is acceptable.

        Extraction is the receiver's single most dangerous operation, so the member
        names are validated before anything is written and the extraction itself uses
        the `data` filter, which refuses absolute paths, traversal, links, and
        devices.
        """
        if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
            return Receipt(Status.REJECT_LAYOUT, detail="archive too large")

        try:
            with tarfile.open(archive_path) as tar:
                members = tar.getmembers()
                if len(members) > MAX_MEMBERS:
                    return Receipt(Status.REJECT_LAYOUT, detail="too many members")
                for member in members:
                    if not member.isfile():
                        return Receipt(Status.REJECT_LAYOUT, detail=f"not a file: {member.name}")
                    if bad := _bad_member_name(member.name):
                        return Receipt(Status.REJECT_LAYOUT, detail=bad)
                target.mkdir(parents=True)
                tar.extractall(target, filter="data")
        except (tarfile.TarError, OSError) as exc:
            return Receipt(Status.REJECT_LAYOUT, detail=f"unreadable archive: {exc}")

        for required in ("manifest.json", "manifest.sig"):
            if not (target / required).is_file():
                return Receipt(Status.REJECT_LAYOUT, detail=f"missing {required}")
        return None

    def _verify(self, unpacked: Path) -> Receipt:
        manifest_bytes = (unpacked / "manifest.json").read_bytes()

        # Read as data, validate as a shape. Nothing here is acted on until step 2.
        try:
            manifest = json.loads(manifest_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # UnicodeDecodeError, not just JSONDecodeError: json.loads on bytes that
            # are not valid UTF-8 raises the former, and these bytes are attacker
            # controlled. Catching only the obvious one turns malformed input into a
            # crash in the component with the most exposure.
            return Receipt(Status.REJECT_LAYOUT, detail=f"manifest not JSON: {exc}")
        schema = json.loads((SCHEMA_DIR / "manifest.schema.json").read_text())
        errors = sorted(
            jsonschema.Draft202012Validator(schema).iter_errors(manifest),
            key=lambda e: list(e.absolute_path),
        )
        if errors:
            return Receipt(Status.REJECT_LAYOUT, detail=f"manifest schema: {errors[0].message}")
        if manifest["format_version"] not in ACCEPTED_FORMAT_VERSIONS:
            return Receipt(Status.REJECT_LAYOUT, detail="unaccepted format_version")

        bundle_id = manifest["bundle_id"]
        signer = manifest["signer_key_id"]

        # Step 2: signature.
        if signer not in self.verify_keys:
            return Receipt(Status.REJECT_SIGNATURE, bundle_id, detail="unknown signer_key_id")
        verify_key, allowed_types = self.verify_keys[signer]
        try:
            verify_key.verify(manifest_bytes, (unpacked / "manifest.sig").read_bytes())
        except BadSignatureError:
            return Receipt(Status.REJECT_SIGNATURE, bundle_id, detail="signature did not verify")
        if manifest["type"] not in allowed_types:
            # §9: a site bundle signed with a dev key, or the reverse.
            return Receipt(Status.REJECT_SIGNATURE, bundle_id, detail="key not valid for this type")

        # Step 3: replay. Checked after the signature so an unsigned bundle can
        # never advance the high-water mark.
        last_seen = self._high_water().get(signer)
        if last_seen is not None and manifest["sequence"] <= last_seen:
            return Receipt(Status.REJECT_REPLAY, bundle_id, detail="sequence not advancing")

        # Step 4: every file, and no others.
        if rejection := self._verify_files(unpacked, manifest, bundle_id):
            return rejection

        # Step 5: lint.
        findings: list[Finding] = lint_bundle(unpacked)
        if findings:
            return Receipt(
                Status.REJECT_LINT,
                bundle_id,
                tuple(sorted({f.code for f in findings})),
                detail="; ".join(str(f) for f in findings),
            )

        self._record_sequence(signer, manifest["sequence"])
        return Receipt(Status.OK, bundle_id)

    def _verify_files(self, unpacked: Path, manifest: dict, bundle_id: str) -> Receipt | None:
        """§4.3: extra files reject, missing files reject, hash mismatch rejects."""
        declared = {entry["path"]: entry for entry in manifest["files"]}
        present = {
            str(p.relative_to(unpacked))
            for p in unpacked.rglob("*")
            if p.is_file() and p.name not in ("manifest.json", "manifest.sig")
        }
        if extra := sorted(present - set(declared)):
            return Receipt(Status.REJECT_HASH_MISMATCH, bundle_id, detail=f"undeclared: {extra[0]}")
        if missing := sorted(set(declared) - present):
            return Receipt(Status.REJECT_HASH_MISMATCH, bundle_id, detail=f"missing: {missing[0]}")

        for path, entry in sorted(declared.items()):
            payload = (unpacked / path).read_bytes()
            if len(payload) != entry["bytes"]:
                return Receipt(Status.REJECT_HASH_MISMATCH, bundle_id, detail=f"length: {path}")
            if blake3(payload).hexdigest() != entry["blake3"]:
                return Receipt(Status.REJECT_HASH_MISMATCH, bundle_id, detail=f"hash: {path}")
        return None

    def _dispatch(self, unpacked: Path) -> None:
        """Step 6. Dispatches on `type` and nothing else (§4.4)."""
        manifest = json.loads((unpacked / "manifest.json").read_text())
        destination = (
            self.command_inbox if manifest["type"] == "command" else self.worker_inbox
        ) / manifest["bundle_id"]
        if destination.exists():
            _rmtree(destination)
        unpacked.rename(destination)

    def _quarantine(self, archive_path: Path, receipt: Receipt) -> Receipt:
        target = self.quarantine / archive_path.name
        target.write_bytes(archive_path.read_bytes())
        target.with_suffix(".receipt.json").write_text(
            json.dumps(
                {
                    "status": int(receipt.status),
                    "bundle_id": receipt.bundle_id,
                    "lint_codes": list(receipt.lint_codes),
                    "detail": receipt.detail,
                },
                indent=1,
            )
        )
        return receipt


def _bad_member_name(name: str) -> str | None:
    """Reject anything that is not a plain relative path inside the fixed layout."""
    if not name or "\x00" in name:
        # tarfile truncates a member name at a NUL byte, so "\x00" arrives as "" and
        # Path("").parts is empty. Found by the fuzz harness, which is the point of
        # having one: an empty tuple indexed at [0] is a crash, not a rejection.
        return "empty or NUL member name"
    if name.startswith("/") or "\\" in name:
        return f"absolute or windows path: {name}"
    parts = Path(name).parts
    if not parts:
        return f"degenerate member name: {name!r}"
    if any(part in ("..", ".") for part in parts):
        return f"traversal: {name}"
    if parts[0] not in ALLOWED_TOP_LEVEL:
        return f"outside layout: {name}"
    if len(parts) > 2:
        return f"too deep: {name}"
    return None


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)
