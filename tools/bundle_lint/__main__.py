"""bundle-lint CLI.

    uv run python -m tools.bundle_lint <bundle_dir>
    uv run python -m tools.bundle_lint --spec spec/site.json [--suite tests/suite.json]

Exit status is 0 for clean and 14 for findings -- the same integer the receiver puts
on the egress channel for a lint rejection (bundle-format-spec §8.3), so a shell
pipeline and the diode agree on what happened.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .lint import lint_bundle, lint_spec, lint_suite

EXIT_CLEAN = 0
EXIT_LINT_REJECT = 14


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bundle-lint", description=__doc__)
    parser.add_argument("bundle_dir", nargs="?", type=Path, help="unpacked bundle root")
    parser.add_argument("--spec", type=Path, help="lint a spec/site.json directly")
    parser.add_argument("--suite", type=Path, help="lint a tests/suite.json directly")
    parser.add_argument(
        "--json", action="store_true", help="emit findings as JSON for the pipeline"
    )
    args = parser.parse_args(argv)

    if args.bundle_dir:
        assert not (args.spec or args.suite), "pass a bundle dir or --spec/--suite"
        findings = lint_bundle(args.bundle_dir)
    else:
        assert args.spec or args.suite, "nothing to lint"
        spec = json.loads(args.spec.read_text()) if args.spec else None
        findings = lint_spec(spec) if spec else []
        if args.suite:
            findings += lint_suite(json.loads(args.suite.read_text()), spec)

    if args.json:
        json.dump([{"code": f.code, "location": f.location} for f in findings],
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for finding in findings:
            print(finding)

    return EXIT_LINT_REJECT if findings else EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
