"""    uv run python -m tools.recon_check <package_dir>"""

from __future__ import annotations

import sys
from pathlib import Path

from .check import check_package


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__.strip())
        return 2
    report = check_package(Path(args[0]))
    print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
