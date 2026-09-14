"""bundle-build: assemble, encrypt, rewrite handles, lint, sign (spec §8.1.5).

The order matters and is not negotiable:

  1. encrypt every content and index blob, naming each by the BLAKE3 of its
     CIPHERTEXT (§5)
  2. rewrite the spec's and suite's logical slot paths to those handles (§6)
  3. lint the rewritten documents
  4. build and sign the manifest over the final bytes

Linting before the rewrite would lint a document that never ships. Signing before
the lint would sign a document we are about to reject. Anything that fails raises:
a build is either complete and signed or it does not exist. There is no partial
bundle, because a partial bundle is the thing the receiver is forced to reason about.
"""

from __future__ import annotations

import json
import secrets
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

from blake3 import blake3
from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_encrypt
from nacl.public import SealedBox

from tools.bundle_lint import lint_spec, lint_suite

from .keys import KeyRole, SigningIdentity, load_golive_public_key

FORMAT_VERSION = "0.1"
NONCE_BYTES = 24
CONTENT_KEY_BYTES = 32
WRAP_ALGORITHM = "x25519-xsalsa20-poly1305"


class BuildError(Exception):
    """A build failed. Never a partially written bundle."""


@dataclass(frozen=True)
class Slot:
    """One place in the spec or suite that points at a blob.

    `pointer` is where the handle goes, `logical` is what the reconstructor wrote
    there, `role` is the AAD the blob is encrypted under.
    """

    document: str  # "spec" or "suite"
    pointer: tuple[str | int, ...]
    logical: str
    role: str


def canonical_json(obj: object) -> bytes:
    """Byte-exact serialisation. The manifest signature covers these bytes."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _get(doc: object, pointer: tuple[str | int, ...]) -> object:
    for part in pointer:
        doc = doc[part]
    return doc


def _set(doc: object, pointer: tuple[str | int, ...], value: object) -> None:
    for part in pointer[:-1]:
        doc = doc[part]
    doc[pointer[-1]] = value


def find_slots(spec: dict, suite: dict | None) -> list[Slot]:
    """Every blob-bearing slot, derived from the spec's own shape.

    Roles come from the slot, never from the reconstructor, so a package cannot
    declare a template to be a db_seed and have it encrypted under the wrong AAD.
    """
    slots: list[Slot] = []
    for i, template in enumerate(spec.get("templates", [])):
        slots.append(Slot("spec", ("templates", i, "blob_ref"), template["blob_ref"], "template"))
    for i, asset in enumerate(spec.get("assets", [])):
        slots.append(Slot("spec", ("assets", i, "blob_ref"), asset["blob_ref"], "asset"))
    if "db" in spec:
        slots.append(Slot("spec", ("db", "seed_blob_ref"), spec["db"]["seed_blob_ref"], "db_seed"))
    for i, search in enumerate(spec.get("search", [])):
        for j, shard in enumerate(search["shard_refs"]):
            slots.append(Slot("spec", ("search", i, "shard_refs", j), shard, "bm25_shard"))
    if suite is not None:
        slots.append(Slot("suite", ("fixture_blob_ref",), suite["fixture_blob_ref"], "fixtures"))
    return slots


def encrypt_blob(plaintext: bytes, content_key: bytes, role: str) -> bytes:
    """XChaCha20-Poly1305, random nonce prepended, role as AAD (§5).

    The AAD binding is what stops a blob with a valid ciphertext being swapped into
    a different slot: decryption under the wrong role simply fails.
    """
    assert len(content_key) == CONTENT_KEY_BYTES
    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = crypto_aead_xchacha20poly1305_ietf_encrypt(
        plaintext, role.encode(), nonce, content_key
    )
    return nonce + ciphertext


def build_bundle(
    package_dir: Path,
    output_dir: Path,
    *,
    identity: SigningIdentity,
    golive_public_key_path: Path,
    sequence: int,
    content_key: bytes | None = None,
) -> Path:
    """Build one signed bundle from a reconstructor package. Returns the tar path."""
    if identity.role is not KeyRole.PIPELINE:
        raise BuildError(
            f"site bundles must be signed with a pipeline key, got {identity.role.value}"
        )

    package_dir = Path(package_dir)
    meta = json.loads((package_dir / "build.json").read_text())
    spec = json.loads((package_dir / "spec" / "site.json").read_text())
    suite_path = package_dir / "tests" / "suite.json"
    suite = json.loads(suite_path.read_text()) if suite_path.exists() else None

    site_id, revision = meta["site_id"], meta["revision"]
    bundle_id = f"{site_id}-r{revision}"
    if spec["site_id"] != site_id:
        raise BuildError(f"spec site_id {spec['site_id']} != build.json {site_id}")

    content_key = content_key or secrets.token_bytes(CONTENT_KEY_BYTES)
    staging = Path(output_dir) / f"{bundle_id}.bundle"
    if staging.exists():
        raise BuildError(f"{staging} already exists; a revision is immutable once signed")
    (staging / "content").mkdir(parents=True)
    (staging / "index").mkdir()
    (staging / "spec").mkdir()

    try:
        return _build(
            staging, spec, suite, meta, bundle_id, site_id, revision, package_dir,
            identity, golive_public_key_path, sequence, content_key, output_dir,
        )
    except Exception:
        # A build is either complete and signed or it does not exist. Leaving a
        # half-written directory behind invites someone to ship it.
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _build(
    staging, spec, suite, meta, bundle_id, site_id, revision, package_dir,
    identity, golive_public_key_path, sequence, content_key, output_dir,
) -> Path:

    files: list[dict] = []
    written: dict[str, str] = {}  # logical path -> handle, so shared blobs dedup

    for slot in find_slots(spec, suite):
        source = package_dir / slot.logical
        if not source.is_file():
            raise BuildError(f"slot {slot.pointer} points at missing file {slot.logical}")

        if slot.logical in written:
            handle = written[slot.logical]
        else:
            blob = encrypt_blob(source.read_bytes(), content_key, slot.role)
            digest = blake3(blob).hexdigest()
            subdir, suffix = (
                ("index", "shard") if slot.role == "bm25_shard" else ("content", "blob")
            )
            handle = f"{subdir}/{digest}.{suffix}"
            (staging / handle).write_bytes(blob)
            written[slot.logical] = handle
            files.append(
                {"path": handle, "blake3": digest, "bytes": len(blob), "role": slot.role}
            )

        document = spec if slot.document == "spec" else suite
        _set(document, slot.pointer, handle)

    manifest_files = {entry["path"]: entry["role"] for entry in files}
    findings = lint_spec(spec, manifest_files)
    if suite is not None:
        findings += lint_suite(suite, spec, manifest_files)
    if findings:
        raise BuildError(
            "lint rejected the rewritten package: "
            + ", ".join(f"{f.code}@{f.location}" for f in findings)
        )

    for relative, document in (("spec/site.json", spec), ("tests/suite.json", suite)):
        if document is None:
            continue
        target = staging / relative
        target.parent.mkdir(exist_ok=True)
        payload = canonical_json(document)
        target.write_bytes(payload)
        files.append(
            {
                "path": relative,
                "blake3": blake3(payload).hexdigest(),
                "bytes": len(payload),
                "role": "spec" if relative.startswith("spec/") else "suite",
            }
        )

    sealed = SealedBox(load_golive_public_key(golive_public_key_path)).encrypt(content_key)
    manifest = {
        "format_version": FORMAT_VERSION,
        "bundle_id": bundle_id,
        "type": meta.get("type", "site"),
        "sequence": sequence,
        "created_at": meta["created_at"],
        "signer_key_id": identity.key_id,
        "site_id": site_id,
        "revision": revision,
        "tier": meta["tier"],
        "content_key_wrapped": {
            "recipient_key_id": meta["golive_key_id"],
            "algorithm": WRAP_ALGORITHM,
            "ciphertext_b64": _b64(sealed),
        },
        "files": sorted(files, key=lambda entry: entry["path"]),
    }
    if supersedes := meta.get("supersedes"):
        manifest["supersedes"] = supersedes

    manifest_bytes = canonical_json(manifest)
    (staging / "manifest.json").write_bytes(manifest_bytes)
    (staging / "manifest.sig").write_bytes(identity.sign(manifest_bytes))

    for empty in ("content", "index"):
        directory = staging / empty
        if not any(directory.iterdir()):
            directory.rmdir()

    archive = Path(output_dir) / f"{bundle_id}.bundle.tar"
    with tarfile.open(archive, "w") as tar:
        # Files only, non-recursive. tarfile.add recurses into directories by
        # default, which would emit every member twice; the receiver rejects an
        # archive whose entries do not match the manifest exactly (§4.3).
        for path in sorted(p for p in staging.rglob("*") if p.is_file()):
            tar.add(path, arcname=str(path.relative_to(staging)), recursive=False)
    return archive


def _b64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode()


def build_command_bundle(
    output_dir: Path,
    *,
    identity: SigningIdentity,
    bundle_id: str,
    sequence: int,
    created_at: str,
    command: dict,
) -> Path:
    """Build one signed command bundle (§9). Dev-signed, no content section.

    `command` is `{op, run_id?, config_ref?, params?}` and is validated by the
    receiver's manifest schema and again by the executor's per-op schema; nothing
    here relaxes either. It is deliberately impossible to attach files.
    """
    if identity.role is not KeyRole.DEV:
        raise BuildError(
            f"command bundles must be signed with a dev key, got {identity.role.value}"
        )
    staging = Path(output_dir) / f"{bundle_id}.bundle"
    if staging.exists():
        raise BuildError(f"{staging} already exists; a command is immutable once signed")
    staging.mkdir(parents=True)
    manifest = {
        "format_version": FORMAT_VERSION,
        "bundle_id": bundle_id,
        "type": "command",
        "sequence": sequence,
        "created_at": created_at,
        "signer_key_id": identity.key_id,
        "command": command,
        "files": [],
    }
    manifest_bytes = canonical_json(manifest)
    (staging / "manifest.json").write_bytes(manifest_bytes)
    (staging / "manifest.sig").write_bytes(identity.sign(manifest_bytes))
    archive = Path(output_dir) / f"{bundle_id}.bundle.tar"
    with tarfile.open(archive, "w") as tar:
        for name in ("manifest.json", "manifest.sig"):
            tar.add(staging / name, arcname=name, recursive=False)
    return archive
