"""Stage, rewrite handles, lint, deploy, run the suite, report in full."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from blake3 import blake3

from tools.bundle_build.build import find_slots
from tools.bundle_lint import Finding, lint_spec, lint_suite
from tools.compose_fastapi_sqlite_v1 import ComposeError, compose_app
from tools.golive.runner import PASS, SKIPPED, TestResult, run_suite

WRITER = "recon-check"


@dataclass(frozen=True)
class TestOutcome:
    test_id: str
    code: int
    detail: str

    @property
    def ok(self) -> bool:
        return self.code in (PASS, SKIPPED)


@dataclass
class ReconReport:
    """Everything the reconstructor needs. Outside, so nothing is withheld."""

    site_id: str
    lint: list[Finding] = field(default_factory=list)
    tests: list[TestOutcome] = field(default_factory=list)
    compose_error: str = ""
    missing_files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.lint
            and not self.compose_error
            and not self.missing_files
            and all(t.ok for t in self.tests)
            and bool(self.tests)
        )

    def summary(self) -> str:
        if self.missing_files:
            return f"FAIL: {len(self.missing_files)} slot(s) point at missing files"
        if self.lint:
            return f"FAIL: {len(self.lint)} lint finding(s)"
        if self.compose_error:
            return "FAIL: the spec needs a pattern the generator does not support"
        failed = [t for t in self.tests if not t.ok]
        if failed:
            return f"FAIL: {len(failed)} of {len(self.tests)} tests"
        return f"PASS: {len(self.tests)} tests"


def stage_package(package_dir: Path, staging: Path) -> tuple[dict, dict | None, dict, list[str]]:
    """Copy the package and rewrite logical slot paths to content handles.

    bundle-format-spec §6: a blob_ref is the BLAKE3 of a blob's *ciphertext*, which
    does not exist until bundle-build encrypts. Rather than lint a document that is
    not the one that ships, recon-check performs the same rewrite unencrypted, over
    the plaintext hash. The resulting spec is structurally identical to the signed
    one -- same shape, same handle pattern, same reference checks -- while nothing
    here is encrypted, because outside there is nothing to hide from.
    """
    package_dir, staging = Path(package_dir), Path(staging)
    spec = json.loads((package_dir / "spec" / "site.json").read_text())
    suite_path = package_dir / "tests" / "suite.json"
    suite = json.loads(suite_path.read_text()) if suite_path.exists() else None

    (staging / "content").mkdir(parents=True, exist_ok=True)
    (staging / "index").mkdir(parents=True, exist_ok=True)

    manifest_files: dict[str, str] = {}
    missing: list[str] = []
    written: dict[str, str] = {}

    for slot in find_slots(spec, suite):
        source = package_dir / slot.logical
        if not source.is_file():
            missing.append(slot.logical)
            continue
        if slot.logical not in written:
            payload = source.read_bytes()
            digest = blake3(payload).hexdigest()
            subdir, suffix = (
                ("index", "shard") if slot.role == "bm25_shard" else ("content", "blob")
            )
            handle = f"{subdir}/{digest}.{suffix}"
            (staging / handle).write_bytes(payload)
            written[slot.logical] = handle
            manifest_files[handle] = slot.role
        handle = written[slot.logical]
        document = spec if slot.document == "spec" else suite
        node = document
        for part in slot.pointer[:-1]:
            node = node[part]
        node[slot.pointer[-1]] = handle

    return spec, suite, manifest_files, missing


def recon_check(package_dir: Path, work_dir: Path) -> ReconReport:
    """Run the full check. Returns a report; raises only on a harness bug."""
    package_dir, work_dir = Path(package_dir), Path(work_dir)
    staging = work_dir / "staged"
    if staging.exists():
        shutil.rmtree(staging)

    spec, suite, manifest_files, missing = stage_package(package_dir, staging)
    report = ReconReport(site_id=spec.get("site_id", "unknown"), missing_files=missing)
    if missing:
        return report

    report.lint = lint_spec(spec, manifest_files)
    if suite is not None:
        report.lint += lint_suite(suite, spec, manifest_files)
    if report.lint:
        return report

    fixtures_path = package_dir / "content" / "fixtures.json"
    fixtures = json.loads(fixtures_path.read_text()) if fixtures_path.is_file() else {}

    episode_db = work_dir / "recon.sqlite"
    shutil.copyfile(staging / spec["db"]["seed_blob_ref"], episode_db)

    try:
        site = compose_app(spec, staging, episode_db)
    except ComposeError as exc:
        # The worker would return code 21 and stop. Outside, say what it was: this
        # is a schema gap to file, not a mystery to debug from an enum.
        report.compose_error = str(exc)
        return report

    if suite is None:
        return report

    results: list[TestResult] = run_suite(site, suite, spec, fixtures, writer=WRITER)
    report.tests = [TestOutcome(r.test_id, r.code, r.detail) for r in results]
    return report
