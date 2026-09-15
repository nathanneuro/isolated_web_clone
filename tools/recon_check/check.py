from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from tools.bundle_lint import Finding, lint_spec, lint_suite
from tools.compose_fastapi_sqlite_v1 import ComposeError, classify_spec, compose_app
from tools.golive.runner import run_suite

RESULT_NAMES = {0: "pass", 1: "FAIL", 2: "skip", 3: "ERROR"}


@dataclass(frozen=True)
class TestOutcome:
    test_id: str
    kind: str
    result: str  # pass | FAIL | skip | ERROR


@dataclass
class CheckReport:
    lint: list[Finding] = field(default_factory=list)
    unsupported: tuple[str, ...] = ()
    compose_error: str = ""
    tests: list[TestOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.lint
            and not self.unsupported
            and not self.compose_error
            and all(t.result in ("pass", "skip") for t in self.tests)
        )

    def render(self) -> str:
        lines = []
        for finding in self.lint:
            lines.append(f"lint     {finding}")
        for element in self.unsupported:
            lines.append(f"unsupported  {element}")
        if self.compose_error:
            lines.append(f"compose  {self.compose_error}")
        for t in self.tests:
            lines.append(f"{t.result:5s}  {t.test_id}  {t.kind}")
        lines.append("OK" if self.ok else "NOT OK")
        return "\n".join(lines)


def check_package(package_dir: Path) -> CheckReport:
    """Lint, classify, compose, and run the suite on a plaintext package."""
    package_dir = Path(package_dir)
    report = CheckReport()
    spec = json.loads((package_dir / "spec" / "site.json").read_text())
    suite_path = package_dir / "tests" / "suite.json"
    suite = json.loads(suite_path.read_text()) if suite_path.exists() else None

    # Lint what bundle-build will lint: the spec with its logical slot paths
    # rewritten to handles. The handles are hashes of the plaintext rather than
    # of ciphertext, which is fine, because lint checks shape and role, not bytes.
    linted_spec, linted_suite, manifest_files, missing = _as_built(package_dir, spec, suite)
    report.lint = [Finding("LINT-REF-01", pointer) for pointer in missing]
    report.lint += lint_spec(linted_spec, manifest_files)
    if linted_suite is not None:
        report.lint += lint_suite(linted_suite, linted_spec, manifest_files)
    report.lint = sorted(set(report.lint))
    if report.lint:
        return report

    report.unsupported = classify_spec(spec).unsupported
    if report.unsupported or suite is None:
        return report

    with tempfile.TemporaryDirectory(prefix="recon-check-") as scratch:
        work = Path(scratch) / "site"
        shutil.copytree(package_dir, work)
        try:
            site = compose_app(spec, work, work / spec["db"]["seed_blob_ref"])
        except ComposeError as exc:
            report.compose_error = str(exc)
            return report
        fixtures = json.loads((work / suite["fixture_blob_ref"]).read_text())
        kinds = {t["id"]: t["kind"] for t in suite["tests"]}
        for result in run_suite(site, suite, spec, fixtures):
            report.tests.append(TestOutcome(result.test_id, kinds[result.test_id], RESULT_NAMES[result.code]))
    return report


def _as_built(package_dir: Path, spec: dict, suite: dict | None):
    """Rewrite slot paths to handles the way bundle-build will, without encrypting."""
    import copy

    from blake3 import blake3

    from tools.bundle_build.build import _set, find_slots

    spec, suite = copy.deepcopy(spec), copy.deepcopy(suite)
    manifest_files: dict[str, str] = {}
    missing: list[str] = []
    for slot in find_slots(spec, suite):
        source = package_dir / slot.logical
        if not source.is_file():
            missing.append("/" + "/".join(map(str, slot.pointer)))
            continue
        digest = blake3(source.read_bytes()).hexdigest()
        subdir, suffix = ("index", "shard") if slot.role == "bm25_shard" else ("content", "blob")
        handle = f"{subdir}/{digest}.{suffix}"
        manifest_files[handle] = slot.role
        _set(spec if slot.document == "spec" else suite, slot.pointer, handle)
    return spec, suite, manifest_files, missing
