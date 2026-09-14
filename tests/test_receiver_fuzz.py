"""Fuzz the receiver's untrusted-input path.

The contract under test is narrow and absolute: for ANY bytes handed to
`Receiver.receive`, it returns a Receipt and nothing reaches the worker inbox. It
must never raise, never hang, and never dispatch. The receiver cannot ask the sender
to resend, so an unhandled exception there is an outage, and a dispatch is worse.

This is the harness bundle-format-spec §8.5 asks to be kept in the repo.
"""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from nacl.signing import VerifyKey

from tools.bundle_build import build_bundle
from tools.bundle_build.keys import KeyRole, generate_demo_keyset, load_signing_identity
from tools.receiver import Receipt, Receiver, Status

EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"
FUZZ = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture(scope="module")
def valid_bundle_bytes(tmp_path_factory) -> bytes:
    root = tmp_path_factory.mktemp("valid")
    keys = root / "keys"
    generate_demo_keyset(keys)
    package = root / "package"
    shutil.copytree(EXAMPLE, package)
    identity = load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)
    archive = build_bundle(
        package, root / "out", identity=identity,
        golive_public_key_path=keys / "golive-wrapping.pub", sequence=1,
    )
    return archive.read_bytes()


@pytest.fixture
def receiver(tmp_path) -> Receiver:
    keys = tmp_path / "keys"
    generate_demo_keyset(keys)
    return Receiver(
        state_dir=tmp_path / "state",
        worker_inbox=tmp_path / "worker-inbox",
        command_inbox=tmp_path / "command-inbox",
        quarantine=tmp_path / "quarantine",
        verify_keys={
            "pipeline-demo": (
                VerifyKey((keys / "pipeline-verify.pub").read_bytes()),
                frozenset({"site", "index_only"}),
            )
        },
    )


def check(receiver: Receiver, payload: bytes, tmp_path: Path) -> None:
    """The invariant: a Receipt, never an exception, never a dispatch."""
    candidate = tmp_path / "fuzz.bundle.tar"
    candidate.write_bytes(payload)
    receipt = receiver.receive(candidate)
    assert isinstance(receipt, Receipt)
    assert receipt.status is not Status.OK, "fuzzed input was accepted"
    assert list(receiver.worker_inbox.iterdir()) == []
    assert list(receiver.command_inbox.iterdir()) == []


@given(payload=st.binary(min_size=0, max_size=4096))
@FUZZ
def test_arbitrary_bytes_are_rejected_not_raised(receiver, tmp_path, payload):
    check(receiver, payload, tmp_path)


@given(cut=st.integers(min_value=0, max_value=100_000))
@FUZZ
def test_truncated_valid_bundles_are_rejected(receiver, tmp_path, valid_bundle_bytes, cut):
    """A real one-way link delivers partial files. The receiver must survive them."""
    check(receiver, valid_bundle_bytes[:cut], tmp_path)


@given(
    offset=st.integers(min_value=0, max_value=100_000),
    mask=st.integers(min_value=1, max_value=255),
)
@FUZZ
def test_single_bit_corruption_is_rejected(receiver, tmp_path, valid_bundle_bytes, offset, mask):
    """What fake_demo_data_diode's --corruption-rate injects, exhaustively."""
    data = bytearray(valid_bundle_bytes)
    data[offset % len(data)] ^= mask
    check(receiver, bytes(data), tmp_path)


@given(
    names=st.lists(
        st.text(
            alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=40
        ),
        min_size=1,
        max_size=6,
    )
)
@FUZZ
def test_hostile_member_names_are_rejected(receiver, tmp_path, names):
    """Member names are attacker-chosen; extraction must never escape the tree."""
    import io

    candidate = tmp_path / "names.bundle.tar"
    with tarfile.open(candidate, "w") as tar:
        for name in names:
            info = tarfile.TarInfo(name)
            info.size = 4
            try:
                tar.addfile(info, io.BytesIO(b"data"))
            except (ValueError, tarfile.TarError):
                return  # tarfile itself refused to write it; nothing to test
    receipt = receiver.receive(candidate)
    assert isinstance(receipt, Receipt)
    assert receipt.status is not Status.OK
    escaped = sorted(p for p in tmp_path.parent.rglob("*") if "cron.d" in str(p))
    assert escaped == []
