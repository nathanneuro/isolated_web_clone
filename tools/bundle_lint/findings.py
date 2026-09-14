"""Finding records and the code registries they draw from."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"


@cache
def lint_codes() -> dict[str, str]:
    """The LINT-* registry. Codes are permanent; see schemas/lint-codes.toml."""
    with (SCHEMA_DIR / "lint-codes.toml").open("rb") as fh:
        return tomllib.load(fh)["lint"]


@dataclass(frozen=True, order=True)
class Finding:
    """One lint finding.

    `location` is a JSON pointer into the document. It names *where*, never *what*:
    echoing the offending value would reintroduce the content channel this linter
    exists to close.
    """

    code: str
    location: str

    def __post_init__(self) -> None:
        assert self.code in lint_codes(), f"unregistered lint code: {self.code}"
        assert self.location.startswith("/") or self.location == "", (
            f"location must be a JSON pointer, got {self.location!r}"
        )

    def __str__(self) -> str:
        return f"{self.code} at {self.location or '/'}"
