"""Go-live: decrypt into a sandbox, compose, test, return codes (spec §8.5).

    1. receive {bundle_id, deployment_dir} from the worker
    2. unwrap the content key with the go-live private key
    3. create an isolated serving sandbox
    4. decrypt blobs directly into the sandbox filesystem
    5. decrypt the test fixture into the runner's memory
    6. run the suite
    7. return codes; destroy the fixture plaintext

The rule that keeps this honest is step 7. On failure the worker gets an enum and
per-test codes and nothing else -- no stack trace, no diff, no decrypted output.
That is the constraint somebody will eventually want to relax to "just let it see
the traceback", and relaxing it hands a prompt-injection surface back to an LLM
inside the trusted zone. The traceback exists; a human reads it at the wired
terminal (physical-controls-spec §2.3).

Plaintext never lands outside the sandbox directory, and the content key is never
logged, never written anywhere the worker can read, and never returned in any code.
"""

from __future__ import annotations

import base64
import json
import shutil
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from blake3 import blake3
from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_decrypt
from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, SealedBox

from tools.compose_fastapi_sqlite_v1 import ComposeError, compose_app

from .runner import PASS, SKIPPED, TestResult, run_suite

NONCE_BYTES = 24


class GoLiveStatus(IntEnum):
    """schemas/status-codes.toml."""

    PASS = 30
    TEST_FAIL = 31
    DECRYPT_FAIL = 32
    TIMEOUT = 33
    COMPOSE_UNSUPPORTED = 21


@dataclass(frozen=True)
class GoLiveResult:
    """Everything the worker learns. Integers, and test IDs it already knew."""

    status: GoLiveStatus
    tests: tuple[TestResult, ...] = ()
    detail: str = field(default="", repr=False)  # inside log only; never returned out

    def for_worker(self) -> dict:
        """The literal payload crossing back to the worker. No detail field."""
        return {
            "status": int(self.status),
            "tests": [[t.test_id, t.code] for t in self.tests],
        }


@dataclass
class GoLiveCounters:
    """metrics-registry block 30-33."""

    pass_: int = 0
    fail: int = 0
    sandbox_count: int = 0
    last_duration_s: int = 0

    def as_metrics(self) -> dict[str, int]:
        return {
            "golive.pass": self.pass_,
            "golive.fail": self.fail,
            "golive.sandbox_count": self.sandbox_count,
            "golive.last_duration_s": self.last_duration_s,
        }


class GoLiveService:
    """Holds the go-live private key. The worker cannot read from this process."""

    def __init__(self, private_key_path: Path, sandbox_root: Path) -> None:
        raw = Path(private_key_path).read_bytes()
        assert len(raw) == 32, "go-live X25519 private key must be 32 bytes"
        self._box = SealedBox(PrivateKey(raw))
        self.sandbox_root = Path(sandbox_root)
        self.sandbox_root.mkdir(parents=True, exist_ok=True)
        self.counters = GoLiveCounters()

    def go_live(self, deployment_dir: Path) -> GoLiveResult:
        deployment_dir = Path(deployment_dir)
        manifest = json.loads((deployment_dir / "manifest.json").read_text())
        sandbox = self.sandbox_root / manifest["bundle_id"]
        if sandbox.exists():
            shutil.rmtree(sandbox)
        sandbox.mkdir(parents=True)

        started = time.monotonic()
        try:
            result = self._go_live(deployment_dir, manifest, sandbox)
        except CryptoError as exc:
            shutil.rmtree(sandbox, ignore_errors=True)
            result = GoLiveResult(GoLiveStatus.DECRYPT_FAIL, detail=str(exc))
        except ComposeError as exc:
            shutil.rmtree(sandbox, ignore_errors=True)
            result = GoLiveResult(GoLiveStatus.COMPOSE_UNSUPPORTED, detail=str(exc))
        self.counters.last_duration_s = int(time.monotonic() - started)
        if result.status is GoLiveStatus.PASS:
            self.counters.pass_ += 1
        else:
            self.counters.fail += 1
        self.counters.sandbox_count = sum(1 for p in self.sandbox_root.iterdir() if p.is_dir())
        return result

    def unseal_eval(self, deployment_dir: Path) -> GoLiveResult:
        """An eval bundle: decrypt its pools into a sandbox beside its choreography.

        Nothing is composed or tested; there is no site here. The same key, the
        same rule: plaintext lands only in the sandbox, and the key is never kept.
        """
        deployment_dir = Path(deployment_dir)
        manifest = json.loads((deployment_dir / "manifest.json").read_text())
        assert manifest["type"] == "eval", manifest["type"]
        sandbox = self.sandbox_root / manifest["bundle_id"]
        if sandbox.exists():
            shutil.rmtree(sandbox)
        (sandbox / "spec").mkdir(parents=True)
        try:
            wrapped = base64.b64decode(manifest["content_key_wrapped"]["ciphertext_b64"])
            content_key = self._box.decrypt(wrapped)
            for entry in manifest["files"]:
                if entry["role"] != "page_text":
                    continue
                blob = (deployment_dir / entry["path"]).read_bytes()
                if blake3(blob).hexdigest() != entry["blake3"]:
                    raise CryptoError(f"handle mismatch: {entry['path']}")
                plaintext = crypto_aead_xchacha20poly1305_ietf_decrypt(
                    blob[NONCE_BYTES:], b"page_text", blob[:NONCE_BYTES], content_key
                )
                target = sandbox / entry["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(plaintext)
            del content_key
        except CryptoError as exc:
            shutil.rmtree(sandbox, ignore_errors=True)
            self.counters.fail += 1
            return GoLiveResult(GoLiveStatus.DECRYPT_FAIL, detail=str(exc))
        (sandbox / "spec" / "choreography.json").write_bytes((deployment_dir / "spec" / "choreography.json").read_bytes())
        self.counters.pass_ += 1
        return GoLiveResult(GoLiveStatus.PASS)

    def _go_live(self, deployment_dir: Path, manifest: dict, sandbox: Path) -> GoLiveResult:
        wrapped = base64.b64decode(manifest["content_key_wrapped"]["ciphertext_b64"])
        content_key = self._box.decrypt(wrapped)

        roles = {entry["path"]: entry["role"] for entry in manifest["files"]}
        fixtures: dict = {}
        for path, role in sorted(roles.items()):
            if role in ("spec", "suite", "population"):
                continue
            blob = (deployment_dir / path).read_bytes()
            if blake3(blob).hexdigest() != path.split("/")[1].split(".")[0]:
                return GoLiveResult(GoLiveStatus.DECRYPT_FAIL, detail=f"handle mismatch: {path}")
            plaintext = crypto_aead_xchacha20poly1305_ietf_decrypt(
                blob[NONCE_BYTES:], role.encode(), blob[:NONCE_BYTES], content_key
            )
            if role == "fixtures":
                # Step 5: into the runner's memory, never onto the filesystem.
                fixtures = json.loads(plaintext)
                continue
            target = sandbox / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(plaintext)

        del content_key  # not logged, not stored, not returned

        spec = json.loads((deployment_dir / "spec" / "site.json").read_text())
        suite = json.loads((deployment_dir / "tests" / "suite.json").read_text())
        # The spec is plaintext structure and goes into the sandbox beside the
        # content it describes, so the search engine can mount the site from the
        # sandbox alone and never has to look at a bundle.
        (sandbox / "spec").mkdir(exist_ok=True)
        (sandbox / "spec" / "site.json").write_text(json.dumps(spec, sort_keys=True))
        population_path = deployment_dir / "spec" / "population.json"
        if population_path.is_file():
            # Structure, like the spec: the driver inside reads it from the sandbox
            # and finds its pools beside it, decrypted with everything else.
            (sandbox / "spec" / "population.json").write_bytes(population_path.read_bytes())
        # The suite runs against a scratch copy of the seed. The seed the sandbox
        # serves stays exactly what the bundle carried; a go-live check must not
        # become a row every episode inherits.
        seed = sandbox / spec["db"]["seed_blob_ref"]
        scratch = sandbox / "golive-suite.sqlite"
        shutil.copyfile(seed, scratch)
        try:
            site = compose_app(spec, sandbox, scratch)
            results = run_suite(site, suite, spec, fixtures)
        finally:
            scratch.unlink(missing_ok=True)

        fixtures.clear()  # step 7: destroy the fixture plaintext

        failed = [r for r in results if r.code not in (PASS, SKIPPED)]
        status = GoLiveStatus.TEST_FAIL if failed else GoLiveStatus.PASS
        if failed:
            shutil.rmtree(sandbox, ignore_errors=True)  # on fail, destroy it (§8.5.7)
        return GoLiveResult(status, tuple(results))
