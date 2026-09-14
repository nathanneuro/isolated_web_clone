"""bundle-build: does the envelope actually hold?

The tests that matter here are the crypto ones. The whole design rests on the claim
that the inside worker can verify a bundle completely and read none of it, and on the
claim that a blob cannot be moved between roles. Both are asserted directly.
"""

from __future__ import annotations

import base64
import json
import shutil
import tarfile
from pathlib import Path

import pytest
from blake3 import blake3
from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_decrypt
from nacl.exceptions import BadSignatureError, CryptoError
from nacl.public import PrivateKey, SealedBox
from nacl.signing import VerifyKey

from tools.bundle_build import BuildError, build_bundle, canonical_json, encrypt_blob
from tools.bundle_build.keys import KeyRole, generate_demo_keyset, load_signing_identity
from tools.bundle_lint import lint_spec, lint_suite

EXAMPLE = Path(__file__).resolve().parents[1] / "example" / "synthetic_site"


@pytest.fixture(scope="module")
def keys(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("keys")
    generate_demo_keyset(directory)
    return directory


@pytest.fixture
def package(tmp_path) -> Path:
    target = tmp_path / "package"
    shutil.copytree(EXAMPLE, target)
    return target


def pipeline_identity(keys: Path):
    return load_signing_identity(keys / "pipeline-signing.key", "pipeline-demo", KeyRole.PIPELINE)


def build(package: Path, keys: Path, out: Path, **kw):
    return build_bundle(
        package,
        out,
        identity=kw.pop("identity", None) or pipeline_identity(keys),
        golive_public_key_path=keys / "golive-wrapping.pub",
        sequence=kw.pop("sequence", 1),
        **kw,
    )


@pytest.fixture
def built(package, keys, tmp_path):
    archive = build(package, keys, tmp_path / "out")
    return archive.parent / archive.name.removesuffix(".tar")


class TestLayout:
    def test_archive_matches_the_manifest_exactly(self, built):
        """§4.3: extra files reject, missing files reject. So emit neither."""
        manifest = json.loads((built / "manifest.json").read_text())
        with tarfile.open(built.with_suffix(".bundle.tar")) as tar:
            members = sorted(m.name for m in tar.getmembers() if m.isfile())
        expected = sorted(
            [e["path"] for e in manifest["files"]] + ["manifest.json", "manifest.sig"]
        )
        assert members == expected

    def test_no_duplicate_archive_members(self, built):
        with tarfile.open(built.with_suffix(".bundle.tar")) as tar:
            names = [m.name for m in tar.getmembers()]
        assert len(names) == len(set(names))

    def test_only_layout_paths_appear(self, built):
        manifest = json.loads((built / "manifest.json").read_text())
        for entry in manifest["files"]:
            assert entry["path"].split("/")[0] in {"content", "index", "spec", "tests"}


class TestSigning:
    def test_signature_verifies_over_exact_manifest_bytes(self, built, keys):
        verify = VerifyKey((keys / "pipeline-verify.pub").read_bytes())
        verify.verify(
            (built / "manifest.json").read_bytes(), (built / "manifest.sig").read_bytes()
        )

    def test_tampered_manifest_fails_verification(self, built, keys):
        verify = VerifyKey((keys / "pipeline-verify.pub").read_bytes())
        manifest = json.loads((built / "manifest.json").read_text())
        manifest["sequence"] += 1
        with pytest.raises(BadSignatureError):
            verify.verify(canonical_json(manifest), (built / "manifest.sig").read_bytes())

    def test_dev_key_cannot_sign_a_site_bundle(self, package, keys, tmp_path):
        """§9: dev signing keys are distinct from pipeline keys, both directions."""
        dev = load_signing_identity(keys / "dev-signing.key", "dev-demo", KeyRole.DEV)
        with pytest.raises(BuildError, match="pipeline key"):
            build(package, keys, tmp_path / "out", identity=dev)

    def test_file_hashes_are_of_the_bytes_on_disk(self, built):
        manifest = json.loads((built / "manifest.json").read_text())
        for entry in manifest["files"]:
            payload = (built / entry["path"]).read_bytes()
            assert blake3(payload).hexdigest() == entry["blake3"], entry["path"]
            assert len(payload) == entry["bytes"], entry["path"]


class TestContentOpacity:
    """The core claim: verifiable in full, readable not at all."""

    def test_no_plaintext_leaks_into_any_blob(self, built):
        """The seed DB's SQLite header must not survive encryption."""
        manifest = json.loads((built / "manifest.json").read_text())
        for entry in manifest["files"]:
            if entry["path"].startswith(("content/", "index/")):
                assert b"SQLite format" not in (built / entry["path"]).read_bytes()

    def test_structure_carries_no_content(self, built):
        """Everything the worker CAN read is identifiers, enums, and handles."""
        spec = (built / "spec" / "site.json").read_text()
        seed = json.loads((EXAMPLE / "content" / "fixtures.json").read_text())
        assert seed["fx_q1_doc"] not in spec
        assert "Longhouse" not in spec

    def test_blob_decrypts_under_its_declared_role(self, built, keys):
        content_key = self._unwrap(built, keys)
        manifest = json.loads((built / "manifest.json").read_text())
        entry = next(e for e in manifest["files"] if e["role"] == "db_seed")
        blob = (built / entry["path"]).read_bytes()
        plaintext = crypto_aead_xchacha20poly1305_ietf_decrypt(
            blob[24:], b"db_seed", blob[:24], content_key
        )
        assert plaintext.startswith(b"SQLite format 3")

    def test_blob_cannot_be_moved_to_another_role(self, built, keys):
        """§5: role is the AAD, so a role swap fails even with valid ciphertext."""
        content_key = self._unwrap(built, keys)
        manifest = json.loads((built / "manifest.json").read_text())
        entry = next(e for e in manifest["files"] if e["role"] == "db_seed")
        blob = (built / entry["path"]).read_bytes()
        with pytest.raises(CryptoError):
            crypto_aead_xchacha20poly1305_ietf_decrypt(
                blob[24:], b"template", blob[:24], content_key
            )

    def test_content_key_is_sealed_to_golive_only(self, built, keys):
        """Nothing outside can open a bundle it just built."""
        manifest = json.loads((built / "manifest.json").read_text())
        sealed = base64.b64decode(manifest["content_key_wrapped"]["ciphertext_b64"])
        with pytest.raises(CryptoError):
            SealedBox(PrivateKey.generate()).decrypt(sealed)
        assert len(self._unwrap(built, keys)) == 32

    @staticmethod
    def _unwrap(built: Path, keys: Path) -> bytes:
        manifest = json.loads((built / "manifest.json").read_text())
        sealed = base64.b64decode(manifest["content_key_wrapped"]["ciphertext_b64"])
        private = PrivateKey((keys / "golive-wrapping.key").read_bytes())
        return SealedBox(private).decrypt(sealed)


class TestHandleRewrite:
    """spec §6: the reconstructor writes logical paths; the builder assigns handles."""

    def test_every_logical_path_is_rewritten(self, built):
        spec = json.loads((built / "spec" / "site.json").read_text())
        suite = json.loads((built / "tests" / "suite.json").read_text())
        refs = (
            [t["blob_ref"] for t in spec["templates"]]
            + [a["blob_ref"] for a in spec["assets"]]
            + [spec["db"]["seed_blob_ref"]]
            + spec["search"][0]["shard_refs"]
            + [suite["fixture_blob_ref"]]
        )
        for ref in refs:
            assert "/" in ref and ref.split("/")[1].split(".")[0].isalnum()
            assert len(ref.split("/")[1].split(".")[0]) == 64, ref

    def test_handles_are_the_hash_of_the_ciphertext(self, built):
        spec = json.loads((built / "spec" / "site.json").read_text())
        ref = spec["db"]["seed_blob_ref"]
        assert blake3((built / ref).read_bytes()).hexdigest() == ref.split("/")[1][:64]

    def test_rewritten_documents_lint_clean(self, built):
        manifest = json.loads((built / "manifest.json").read_text())
        roles = {e["path"]: e["role"] for e in manifest["files"]}
        spec = json.loads((built / "spec" / "site.json").read_text())
        suite = json.loads((built / "tests" / "suite.json").read_text())
        assert lint_spec(spec, roles) == []
        assert lint_suite(suite, spec, roles) == []


class TestFailClosed:
    def test_missing_slot_file_is_a_build_error(self, package, keys, tmp_path):
        (package / "content" / "seed.sqlite").unlink()
        with pytest.raises(BuildError, match="missing file"):
            build(package, keys, tmp_path / "out")

    def test_lint_failure_is_a_build_error_not_a_warning(self, package, keys, tmp_path):
        spec = json.loads((package / "spec" / "site.json").read_text())
        spec["routes"][0]["template"] = "t_does_not_exist"
        (package / "spec" / "site.json").write_text(json.dumps(spec))
        with pytest.raises(BuildError, match="LINT-REF-03"):
            build(package, keys, tmp_path / "out")

    def test_failed_build_leaves_nothing_behind(self, package, keys, tmp_path):
        (package / "content" / "seed.sqlite").unlink()
        out = tmp_path / "out"
        with pytest.raises(BuildError):
            build(package, keys, out)
        assert not (out / "site-000001-r1.bundle").exists()

    def test_revision_is_immutable(self, package, keys, tmp_path):
        out = tmp_path / "out"
        build(package, keys, out)
        with pytest.raises(BuildError, match="immutable"):
            build(package, keys, out)

    def test_site_id_disagreement_is_a_build_error(self, package, keys, tmp_path):
        spec = json.loads((package / "spec" / "site.json").read_text())
        spec["site_id"] = "site-000999"
        (package / "spec" / "site.json").write_text(json.dumps(spec))
        with pytest.raises(BuildError, match="site_id"):
            build(package, keys, tmp_path / "out")


class TestEncryptBlob:
    def test_nonce_is_fresh_per_call(self):
        key = b"k" * 32
        assert encrypt_blob(b"same", key, "asset") != encrypt_blob(b"same", key, "asset")

    def test_ciphertext_is_longer_than_plaintext_by_nonce_and_tag(self):
        blob = encrypt_blob(b"x" * 100, b"k" * 32, "asset")
        assert len(blob) == 100 + 24 + 16
