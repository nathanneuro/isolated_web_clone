"""Key material handling for the outside pipeline.

Three key pairs appear in this design and they must not be confused:

  pipeline signing (Ed25519)   outside. Signs `site` and `index_only` bundles. The
                               inside holds only the verification half.
  dev signing (Ed25519)        outside, held by a developer. Signs `command` bundles.
                               A site bundle signed with a dev key is rejected, and
                               vice versa (bundle-format-spec §9).
  go-live wrapping (X25519)    the PUBLIC half lives outside so bundle-build can seal
                               content keys to it. The private half exists only
                               inside, in the go-live service. Nothing outside can
                               decrypt a bundle it has built.

Secret keys are never written into the repository. `keys/` is gitignored and the
demo keygen below is for the single-machine walkthrough only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from nacl.public import PrivateKey, PublicKey
from nacl.signing import SigningKey, VerifyKey


class KeyRole(Enum):
    PIPELINE = "pipeline"  # signs site and index_only bundles
    DEV = "dev"  # signs command bundles
    GOLIVE = "golive"  # receives sealed content keys


@dataclass(frozen=True)
class SigningIdentity:
    """A signing key plus the key_id that names it in manifests."""

    key_id: str
    role: KeyRole
    signing_key: SigningKey

    def sign(self, message: bytes) -> bytes:
        return self.signing_key.sign(message).signature

    @property
    def verify_key(self) -> VerifyKey:
        return self.signing_key.verify_key


def load_signing_identity(path: Path, key_id: str, role: KeyRole) -> SigningIdentity:
    raw = path.read_bytes()
    assert len(raw) == 32, f"{path}: Ed25519 seed must be 32 bytes, got {len(raw)}"
    return SigningIdentity(key_id, role, SigningKey(raw))


def load_verify_key(path: Path) -> VerifyKey:
    raw = path.read_bytes()
    assert len(raw) == 32, f"{path}: Ed25519 public key must be 32 bytes"
    return VerifyKey(raw)


def load_golive_public_key(path: Path) -> PublicKey:
    raw = path.read_bytes()
    assert len(raw) == 32, f"{path}: X25519 public key must be 32 bytes"
    return PublicKey(raw)


def generate_demo_keyset(directory: Path) -> dict[str, Path]:
    """Generate a throwaway keyset for the single-machine walkthrough.

    NOT for any deployment. Real pipeline and dev signing keys are generated on the
    machines that hold them and never travel; the go-live private key is generated
    INSIDE the airgap and its public half is the only piece that comes out.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    for role in (KeyRole.PIPELINE, KeyRole.DEV):
        signing = SigningKey.generate()
        secret = directory / f"{role.value}-signing.key"
        public = directory / f"{role.value}-verify.pub"
        secret.write_bytes(bytes(signing))
        secret.chmod(0o600)
        public.write_bytes(bytes(signing.verify_key))
        written[f"{role.value}-signing"] = secret
        written[f"{role.value}-verify"] = public

    golive = PrivateKey.generate()
    secret = directory / "golive-wrapping.key"
    public = directory / "golive-wrapping.pub"
    secret.write_bytes(bytes(golive))
    secret.chmod(0o600)
    public.write_bytes(bytes(golive.public_key))
    written["golive-private"] = secret
    written["golive-public"] = public

    (directory / "DEMO_KEYS_DO_NOT_DEPLOY").write_text(
        "Throwaway keys for the single-machine walkthrough.\n"
        "Real go-live private keys are generated inside the airgap and never leave.\n"
        "Real signing keys never enter it.\n"
    )
    return written
