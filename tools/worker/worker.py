from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Protocol

from tools.bundle_build.build import find_slots
from tools.compose_fastapi_sqlite_v1 import classify_spec
from tools.golive import GoLiveResult, GoLiveService
from tools.golive.runner import RUNNER_ERROR
from tools.registry import SiteRegistry

MAX_ATTEMPTS = 3
TIER_FRAMEWORK = {"A": "fastapi-sqlite-v1", "B": "static-v1"}
ACCEPTED_TYPES = frozenset({"site", "index_only"})


class WorkerStatus(IntEnum):
    """schemas/status-codes.toml. The worker emits these and nothing else."""

    COMPOSED = 20
    COMPOSE_FAILED = 21
    GOLIVE_PASS = 30
    GOLIVE_TEST_FAIL = 31
    GOLIVE_DECRYPT_FAIL = 32
    GOLIVE_TIMEOUT = 33
    LIVE = 40


# Subcodes, from the skill's § Codes. Zero means "no subcode".
UNSUPPORTED_ELEMENT = 1
SLOT_MAP_INCONSISTENT = 2
EXCESSIVE_PATTERN_FLAGS = 3
MOUNT_INCONSISTENT = 4

RENDER_DIFF_ONLY = 1
EXTERNAL_REQUEST = 2
RETRIES_EXHAUSTED = 3
RUNNER_ERROR_PERSISTED = 4


@dataclass(frozen=True)
class StatusEmission:
    """Every status emission is `{bundle_id, status_code, subcode?, attempt}`. Nothing else."""

    bundle_id: str
    status_code: WorkerStatus
    subcode: int = 0
    attempt: int = 1


class PatternChooser(Protocol):
    """Where a model would choose among a FLAGGED pattern's options.

    Receives identifiers and returns one of them. There is no argument through
    which content could reach it.
    """

    def __call__(self, pattern_id: str, options: tuple[str, ...]) -> str: ...


@dataclass
class WorkerCounters:
    inbox_depth: int = 0
    composed: int = 0
    failed: int = 0
    last_status_code: int = 0
    last_subcode: int = 0

    def as_metrics(self) -> dict[str, int]:
        return {f"worker.{k}": v for k, v in vars(self).items()}


class Worker:
    """Consumes the receiver's inbox in sequence order, one bundle at a time."""

    def __init__(
        self,
        inbox: Path,
        work_dir: Path,
        golive: GoLiveService,
        registry: SiteRegistry,
        *,
        chooser: PatternChooser | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        assert 1 <= max_attempts <= MAX_ATTEMPTS, max_attempts
        self.inbox = Path(inbox)
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._golive = golive
        self._registry = registry
        self._chooser = chooser
        self.max_attempts = max_attempts
        self.counters = WorkerCounters()
        self.emissions: list[StatusEmission] = []

    # -- inbox -----------------------------------------------------------------

    def pending(self) -> list[str]:
        """Bundle ids in the inbox, lowest sequence first (the order they were signed)."""
        entries = []
        for path in self.inbox.iterdir():
            if path.is_dir() and (path / "manifest.json").is_file():
                manifest = json.loads((path / "manifest.json").read_text())
                entries.append((manifest["sequence"], path.name))
        self.counters.inbox_depth = len(entries)
        return [name for _, name in sorted(entries)]

    def run_once(self) -> StatusEmission | None:
        """Process the next bundle, if any. Returns its final emission."""
        queue = self.pending()
        if not queue:
            return None
        return self.process(queue[0])

    # -- one bundle ------------------------------------------------------------

    def process(self, bundle_id: str) -> StatusEmission:
        source = self.inbox / bundle_id
        try:
            final = self._process(bundle_id, source)
        finally:
            # Whatever happened, the bundle leaves the inbox. A bundle that crashed
            # the worker must not be retried forever; a human triages it from work/.
            if source.exists():
                shutil.rmtree(source)
            self.counters.inbox_depth = max(0, self.counters.inbox_depth - 1)
        return final

    def _process(self, bundle_id: str, source: Path) -> StatusEmission:
        manifest = json.loads((source / "manifest.json").read_text())
        assert manifest["bundle_id"] == bundle_id, "inbox directory does not match its manifest"

        # 1. Load and sanity-check. The receiver checked all of this; a disagreement
        # here means the mount is inconsistent, which is subcode 4.
        if manifest["type"] not in ACCEPTED_TYPES:
            return self._fail(bundle_id, WorkerStatus.COMPOSE_FAILED, MOUNT_INCONSISTENT)
        spec = json.loads((source / "spec" / "site.json").read_text())
        if spec.get("framework") != TIER_FRAMEWORK.get(manifest["tier"]):
            return self._fail(bundle_id, WorkerStatus.COMPOSE_FAILED, MOUNT_INCONSISTENT)
        suite_path = source / "tests" / "suite.json"
        if not suite_path.is_file():
            # The go-live service tests before it serves; a bundle it cannot test
            # cannot go live. Nothing in this generator ships an untested site.
            return self._fail(bundle_id, WorkerStatus.COMPOSE_FAILED, UNSUPPORTED_ELEMENT)
        suite = json.loads(suite_path.read_text())
        roles = {entry["path"]: entry["role"] for entry in manifest["files"]}
        slots = find_slots(spec, suite)
        for slot in slots:
            if roles.get(slot.logical) != slot.role:
                return self._fail(bundle_id, WorkerStatus.COMPOSE_FAILED, MOUNT_INCONSISTENT)

        # 2. Classify.
        classification = classify_spec(spec)
        if classification.unsupported:
            return self._fail(bundle_id, WorkerStatus.COMPOSE_FAILED, UNSUPPORTED_ELEMENT)
        if classification.flagged:
            if self._chooser is None:
                return self._fail(bundle_id, WorkerStatus.COMPOSE_FAILED, EXCESSIVE_PATTERN_FLAGS)
            raise NotImplementedError("the generator has no FLAGGED patterns yet")

        # 3. Compose. The deployment is the verified bundle plus a slot map and a
        # deploy record. Blobs are copied as the ciphertext they arrived as.
        deployment = self.work_dir / bundle_id
        if deployment.exists():
            shutil.rmtree(deployment)
        shutil.copytree(source, deployment)
        slot_map = {slot.logical: {"role": slot.role, "slot": "/".join(map(str, slot.pointer))} for slot in slots}
        blobs = {path for path, role in roles.items() if role not in ("spec", "suite")}
        if set(slot_map) != blobs:
            return self._fail(bundle_id, WorkerStatus.COMPOSE_FAILED, SLOT_MAP_INCONSISTENT)
        (deployment / "slots.json").write_text(json.dumps(slot_map, indent=1, sort_keys=True))
        (deployment / "deploy.json").write_text(
            json.dumps(
                {
                    "bundle_id": bundle_id,
                    "site_id": manifest["site_id"],
                    "revision": manifest["revision"],
                    "framework": spec["framework"],
                    "hostname": spec["hostname"],
                    "supersedes": manifest.get("supersedes"),
                },
                indent=1,
            )
        )
        self._emit(bundle_id, WorkerStatus.COMPOSED)
        self.counters.composed += 1

        # 4 and 5. Go-live, with the retry rules and nothing else.
        kinds = {test["id"]: test["kind"] for test in suite["tests"]}
        timeout_retried = runner_retried = False
        for attempt in range(1, self.max_attempts + 1):
            result = self._golive.go_live(deployment)
            status = int(result.status)

            if status == WorkerStatus.GOLIVE_PASS:
                return self._register(bundle_id, manifest, spec, deployment, attempt)
            if status == WorkerStatus.GOLIVE_DECRYPT_FAIL:
                return self._fail(bundle_id, WorkerStatus.GOLIVE_DECRYPT_FAIL, attempt=attempt)
            if status == WorkerStatus.COMPOSE_FAILED:
                return self._fail(bundle_id, WorkerStatus.COMPOSE_FAILED, UNSUPPORTED_ELEMENT, attempt)
            if status == WorkerStatus.GOLIVE_TIMEOUT:
                if timeout_retried:
                    return self._fail(bundle_id, WorkerStatus.GOLIVE_TIMEOUT, attempt=attempt)
                timeout_retried = True
                continue

            assert status == WorkerStatus.GOLIVE_TEST_FAIL, status
            subcode = self._retry_rule(result, kinds, runner_retried)
            if subcode is None:
                runner_retried = True  # the only retry rule the generator can act on
                continue
            return self._fail(bundle_id, WorkerStatus.GOLIVE_TEST_FAIL, subcode, attempt)

        return self._fail(bundle_id, WorkerStatus.GOLIVE_TEST_FAIL, RETRIES_EXHAUSTED, self.max_attempts)

    def _retry_rule(self, result: GoLiveResult, kinds: dict[str, str], runner_retried: bool) -> int | None:
        """Map failing test kinds to what the skill permits. None means retry once, unchanged."""
        failed = [t for t in result.tests if t.code not in (0, 2)]
        failed_kinds = {kinds[t.test_id] for t in failed}
        if "no_external_requests" in failed_kinds:
            return EXTERNAL_REQUEST  # a template reaches out; never retried
        if failed_kinds == {"render_diff"}:
            return RENDER_DIFF_ONLY  # soft signal; a human decides on the threshold
        if any(t.code == RUNNER_ERROR for t in failed):
            return RUNNER_ERROR_PERSISTED if runner_retried else None
        # Every other rule maps a failing kind to an alternate pattern choice, and
        # this generator has none. Do not improvise.
        return 0

    def _register(self, bundle_id: str, manifest: dict, spec: dict, deployment: Path, attempt: int) -> StatusEmission:
        self._emit(bundle_id, WorkerStatus.GOLIVE_PASS, attempt=attempt)
        self._registry.register(
            site_id=manifest["site_id"],
            revision=manifest["revision"],
            bundle_id=bundle_id,
            hostname=spec["hostname"],
            deployment=self._golive.sandbox_root / bundle_id,
            supersedes=manifest.get("supersedes"),
        )
        return self._emit(bundle_id, WorkerStatus.LIVE, attempt=attempt)

    def _fail(self, bundle_id: str, status: WorkerStatus, subcode: int = 0, attempt: int = 1) -> StatusEmission:
        self.counters.failed += 1
        return self._emit(bundle_id, status, subcode, attempt)

    def _emit(self, bundle_id: str, status: WorkerStatus, subcode: int = 0, attempt: int = 1) -> StatusEmission:
        emission = StatusEmission(bundle_id, status, subcode, attempt)
        self.emissions.append(emission)
        self.counters.last_status_code = int(status)
        self.counters.last_subcode = subcode
        return emission
