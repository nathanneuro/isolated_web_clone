"""recon-check CLI.

    uv run python -m tools.recon_check <package_dir>

Exit 0 when the package is ready to hand to the QA agent, 1 otherwise. A package
that fails this is not handed on (site-reconstruct SKILL.md §6).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from tools.golive.runner import FAIL, PASS, RUNNER_ERROR, SKIPPED

from .check import recon_check

LABEL = {PASS: "pass", FAIL: "FAIL", SKIPPED: "skip", RUNNER_ERROR: "ERROR"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recon-check", description=__doc__)
    parser.add_argument("package_dir", type=Path)
    parser.add_argument("--work-dir", type=Path, help="defaults to a temp directory")
    parser.add_argument("--quiet", action="store_true", help="summary line only")
    args = parser.parse_args(argv)

    work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix="recon-check-"))
    report = recon_check(args.package_dir, work_dir)

    if not args.quiet:
        print(f"site: {report.site_id}")
        for path in report.missing_files:
            print(f"  missing file: {path}")
        for finding in report.lint:
            print(f"  lint {finding}")
        if report.compose_error:
            print(f"  compose: {report.compose_error}")
        for outcome in report.tests:
            print(f"  {outcome.test_id}  {LABEL[outcome.code]:<5}  {outcome.detail}")

    print(report.summary())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
