"""Receiver: does it actually fail closed?

This is the component an attacker reaches first and the one with the least
authority, so the tests are mostly attempts to get something past it. Each test
asserts both that the bundle was rejected and that it landed in quarantine rather
than in the worker's inbox: "rejected but processed anyway" is the failure that
would matter.
"""

from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path

import pytest
from blake3 import blake3
from nacl.signing import VerifyKey

from tools.bundle_build import build_bundle, canonical_json
from tools.bundle_build.keys import KeyRole, generate_demo_keyset, load_signing_identity
from tools.receiver import Receipt, Receiver, Status

EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"


@pytest.fixture(scope="module")
def keys(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("keys")
    generate_demo_keyset(directory)
    return directory


@pytest.fixture
def receiver(tmp_path, keys) -> Receiver:
    return Receiver(
        state_dir=tmp_path / "state",
        worker_inbox=tmp_path / "worker-inbox",
        command_inbox=tmp_path / "command-inbox",
        quarantine=tmp_path / "quarantine",
        verify_keys={
            "pipeline-demo": (
                VerifyKey((keys / "pipeline-verify.pub").read_bytes()),
                frozenset({"site", "index_only"}),
            ),
            "dev-demo": (
                VerifyKey((keys / "dev-verify.pub").read_bytes()),
                frozenset({"command"}),
            ),
        },
    )


@pytest.fixture
def bundle(tmp_path, keys) -> Path:
    package = tmp_path / "package"
    shutil.copytree(EXAMPLE, package)
    identity = load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)
    return build_bundle(
        package,
        tmp_path / "out",
        identity=identity,
        golive_public_key_path=keys / "golive-wrapping.pub",
        sequence=100,
    )


def repack(bundle: Path, mutate) -> Path:
    """Unpack, let `mutate` alter the tree, repack. How an attacker edits a bundle."""
    work = bundle.parent / "tamper"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    with tarfile.open(bundle) as tar:
        tar.extractall(work, filter="data")
    mutate(work)
    tampered = bundle.parent / "tampered.bundle.tar"
    tampered.unlink(missing_ok=True)
    with tarfile.open(tampered, "w") as tar:
        for path in sorted(p for p in work.rglob("*") if p.is_file()):
            tar.add(path, arcname=str(path.relative_to(work)), recursive=False)
    return tampered


def assert_quarantined(receiver: Receiver, receipt: Receipt, status: Status) -> None:
    assert receipt.status is status, receipt.detail
    assert not receipt.accepted
    assert list(receiver.worker_inbox.iterdir()) == [], "rejected bundle was dispatched"
    assert list(receiver.command_inbox.iterdir()) == []
    assert any(receiver.quarantine.iterdir()), "rejected bundle was not quarantined"


class TestHappyPath:
    def test_valid_bundle_is_accepted_and_dispatched(self, receiver, bundle):
        receipt = receiver.receive(bundle)
        assert receipt.status is Status.OK
        assert receipt.bundle_id == "site-000001-r1"
        assert [p.name for p in receiver.worker_inbox.iterdir()] == ["site-000001-r1"]
        assert list(receiver.quarantine.iterdir()) == []

    def test_dispatched_tree_is_complete(self, receiver, bundle):
        receiver.receive(bundle)
        delivered = receiver.worker_inbox / "site-000001-r1"
        assert (delivered / "spec" / "site.json").is_file()
        assert (delivered / "tests" / "suite.json").is_file()
        assert (delivered / "manifest.json").is_file()

    def test_receipt_carries_no_content(self, receiver, bundle):
        """A receipt crosses to the dashboard as an integer; assert it can."""
        receipt = receiver.receive(bundle)
        assert isinstance(int(receipt.status), int)
        assert receipt.lint_codes == ()


class TestSignature:
    def test_tampered_spec_is_caught_by_the_hash(self, receiver, bundle):
        def mutate(root: Path):
            spec = json.loads((root / "spec" / "site.json").read_text())
            spec["hostname"] = "evil-000001.internal"
            (root / "spec" / "site.json").write_bytes(canonical_json(spec))

        assert_quarantined(receiver, receiver.receive(repack(bundle, mutate)), Status.REJECT_HASH_MISMATCH)

    def test_tampered_spec_with_repaired_hash_is_caught_by_the_signature(self, receiver, bundle):
        """The attacker fixes the manifest to match. The signature still fails."""

        def mutate(root: Path):
            spec_path = root / "spec" / "site.json"
            spec = json.loads(spec_path.read_text())
            spec["hostname"] = "evil-000001.internal"
            payload = canonical_json(spec)
            spec_path.write_bytes(payload)
            manifest = json.loads((root / "manifest.json").read_text())
            for entry in manifest["files"]:
                if entry["path"] == "spec/site.json":
                    entry["blake3"] = blake3(payload).hexdigest()
                    entry["bytes"] = len(payload)
            (root / "manifest.json").write_bytes(canonical_json(manifest))

        assert_quarantined(receiver, receiver.receive(repack(bundle, mutate)), Status.REJECT_SIGNATURE)

    def test_unknown_signer_is_rejected(self, receiver, bundle):
        def mutate(root: Path):
            manifest = json.loads((root / "manifest.json").read_text())
            manifest["signer_key_id"] = "pipeline-attacker"
            (root / "manifest.json").write_bytes(canonical_json(manifest))

        assert_quarantined(receiver, receiver.receive(repack(bundle, mutate)), Status.REJECT_SIGNATURE)

    def test_dev_signed_site_bundle_is_rejected(self, tmp_path, keys, receiver):
        """§9: dev keys sign commands, pipeline keys sign sites. Not interchangeable.

        Built by hand rather than via bundle_build, which refuses this at build time;
        the point is that the receiver refuses it independently.
        """
        package = tmp_path / "pkg2"
        shutil.copytree(EXAMPLE, package)
        pipeline = load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)
        built = build_bundle(
            package, tmp_path / "out2", identity=pipeline,
            golive_public_key_path=keys / "golive-wrapping.pub", sequence=7,
        )
        dev = load_signing_identity(keys / "dev-signing.key", "dev-demo", KeyRole.DEV)

        def mutate(root: Path):
            manifest = json.loads((root / "manifest.json").read_text())
            manifest["signer_key_id"] = "dev-demo"
            payload = canonical_json(manifest)
            (root / "manifest.json").write_bytes(payload)
            (root / "manifest.sig").write_bytes(dev.sign(payload))

        receipt = receiver.receive(repack(built, mutate))
        assert_quarantined(receiver, receipt, Status.REJECT_SIGNATURE)
        assert "not valid for this type" in receipt.detail


class TestReplay:
    def test_replaying_the_same_bundle_is_rejected(self, receiver, bundle):
        assert receiver.receive(bundle).status is Status.OK
        second = receiver.receive(bundle)
        assert second.status is Status.REJECT_REPLAY

    def test_lower_sequence_is_rejected(self, tmp_path, keys, receiver):
        package = tmp_path / "pkg3"
        shutil.copytree(EXAMPLE, package)
        identity = load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)
        high = build_bundle(package, tmp_path / "hi", identity=identity,
                            golive_public_key_path=keys / "golive-wrapping.pub", sequence=500)
        low = build_bundle(package, tmp_path / "lo", identity=identity,
                           golive_public_key_path=keys / "golive-wrapping.pub", sequence=499)
        assert receiver.receive(high).status is Status.OK
        assert receiver.receive(low).status is Status.REJECT_REPLAY

    def test_gaps_are_permitted(self, tmp_path, keys, receiver):
        """§4.2: a bundle may be dropped outside; gaps are logged, not rejected."""
        package = tmp_path / "pkg4"
        shutil.copytree(EXAMPLE, package)
        identity = load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)
        first = build_bundle(package, tmp_path / "a", identity=identity,
                             golive_public_key_path=keys / "golive-wrapping.pub", sequence=10)
        later = build_bundle(package, tmp_path / "b", identity=identity,
                             golive_public_key_path=keys / "golive-wrapping.pub", sequence=9999)
        assert receiver.receive(first).status is Status.OK
        assert receiver.receive(later).status is Status.OK

    def test_unsigned_bundle_cannot_advance_the_high_water_mark(self, receiver, bundle, tmp_path, keys):
        """Order matters: signature before sequence, or a forgery poisons the mark."""

        def mutate(root: Path):
            manifest = json.loads((root / "manifest.json").read_text())
            manifest["sequence"] = 10**12
            (root / "manifest.json").write_bytes(canonical_json(manifest))

        assert receiver.receive(repack(bundle, mutate)).status is Status.REJECT_SIGNATURE
        assert receiver.receive(bundle).status is Status.OK, "forgery moved the mark"

    def test_sequence_floor_recovery(self, receiver, bundle):
        """§11.5: restoring a lost mark must block bundles in flight below it."""
        receiver.set_sequence_floor("pipeline-demo", 10**6)
        assert receiver.receive(bundle).status is Status.REJECT_REPLAY


class TestLayout:
    def test_path_traversal_member_is_rejected(self, receiver, bundle, tmp_path):
        evil = tmp_path / "evil.bundle.tar"
        with tarfile.open(bundle) as src, tarfile.open(evil, "w") as dst:
            for member in src.getmembers():
                dst.addfile(member, src.extractfile(member))
            payload = b"pwned"
            info = tarfile.TarInfo("../../etc/cron.d/evil")
            info.size = len(payload)
            import io

            dst.addfile(info, io.BytesIO(payload))
        receipt = receiver.receive(evil)
        assert_quarantined(receiver, receipt, Status.REJECT_LAYOUT)
        assert "traversal" in receipt.detail

    def test_absolute_path_member_is_rejected(self, receiver, bundle, tmp_path):
        import io

        evil = tmp_path / "abs.bundle.tar"
        with tarfile.open(bundle) as src, tarfile.open(evil, "w") as dst:
            for member in src.getmembers():
                dst.addfile(member, src.extractfile(member))
            info = tarfile.TarInfo("/etc/passwd")
            info.size = 3
            dst.addfile(info, io.BytesIO(b"bad"))
        assert_quarantined(receiver, receiver.receive(evil), Status.REJECT_LAYOUT)

    def test_member_outside_the_layout_is_rejected(self, receiver, bundle):
        def mutate(root: Path):
            (root / "README.md").write_text("hello")

        receipt = receiver.receive(repack(bundle, mutate))
        assert_quarantined(receiver, receipt, Status.REJECT_LAYOUT)
        assert "outside layout" in receipt.detail

    def test_undeclared_file_inside_the_layout_is_rejected(self, receiver, bundle):
        """Passes the layout check, fails the manifest's exact-match rule (§4.3)."""

        def mutate(root: Path):
            (root / "content" / ("f" * 64 + ".blob")).write_bytes(b"smuggled")

        receipt = receiver.receive(repack(bundle, mutate))
        assert_quarantined(receiver, receipt, Status.REJECT_HASH_MISMATCH)
        assert "undeclared" in receipt.detail

    def test_missing_declared_file_is_rejected(self, receiver, bundle):
        def mutate(root: Path):
            next(iter((root / "content").iterdir())).unlink()

        receipt = receiver.receive(repack(bundle, mutate))
        assert_quarantined(receiver, receipt, Status.REJECT_HASH_MISMATCH)
        assert "missing" in receipt.detail

    def test_missing_signature_file_is_rejected(self, receiver, bundle):
        def mutate(root: Path):
            (root / "manifest.sig").unlink()

        assert_quarantined(receiver, receiver.receive(repack(bundle, mutate)), Status.REJECT_LAYOUT)

    @pytest.mark.parametrize("name", ["\x00", "", "./", "a\x00b"])
    def test_degenerate_member_names_are_rejected(self, receiver, bundle, tmp_path, name):
        """Regression: tarfile truncates at NUL, so these reach the check as ""."""
        import io

        evil = tmp_path / "degenerate.bundle.tar"
        with tarfile.open(bundle) as src, tarfile.open(evil, "w") as dst:
            for member in src.getmembers():
                dst.addfile(member, src.extractfile(member))
            info = tarfile.TarInfo(name)
            info.size = 3
            try:
                dst.addfile(info, io.BytesIO(b"bad"))
            except (ValueError, tarfile.TarError):
                pytest.skip(f"tarfile refused to write member {name!r}")
        assert_quarantined(receiver, receiver.receive(evil), Status.REJECT_LAYOUT)

    def test_garbage_archive_is_rejected(self, receiver, tmp_path):
        junk = tmp_path / "junk.bundle.tar"
        junk.write_bytes(b"\x00\xff" * 5000)
        assert_quarantined(receiver, receiver.receive(junk), Status.REJECT_LAYOUT)

    def test_manifest_that_is_not_json_is_rejected(self, receiver, bundle):
        def mutate(root: Path):
            (root / "manifest.json").write_bytes(b"\xde\xad\xbe\xef not json")

        assert_quarantined(receiver, receiver.receive(repack(bundle, mutate)), Status.REJECT_LAYOUT)

    def test_manifest_failing_its_schema_is_rejected_before_anything_else(self, receiver, bundle):
        def mutate(root: Path):
            manifest = json.loads((root / "manifest.json").read_text())
            manifest["command"] = {"op": "start_run"}  # site bundles carry no command
            (root / "manifest.json").write_bytes(canonical_json(manifest))

        assert_quarantined(receiver, receiver.receive(repack(bundle, mutate)), Status.REJECT_LAYOUT)

    def test_unaccepted_format_version_is_rejected(self, receiver, bundle):
        """§10: receivers ship with a list of accepted versions and check exactly."""

        def mutate(root: Path):
            manifest = json.loads((root / "manifest.json").read_text())
            manifest["format_version"] = "0.2"
            (root / "manifest.json").write_bytes(canonical_json(manifest))

        assert_quarantined(receiver, receiver.receive(repack(bundle, mutate)), Status.REJECT_LAYOUT)

    def test_manifest_with_invalid_utf8_is_rejected_not_crashed(self, receiver, bundle):
        """json.loads on non-UTF-8 bytes raises UnicodeDecodeError, not
        JSONDecodeError. The receiver must reject, never propagate."""

        def mutate(root: Path):
            (root / "manifest.json").write_bytes(b"\xff\xfe\x00garbage")

        assert_quarantined(receiver, receiver.receive(repack(bundle, mutate)), Status.REJECT_LAYOUT)
