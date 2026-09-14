"""The metric registry, compiled into both ends.

Names never travel on the wire. The registry is what turns an integer back into a
named, scaled quantity, and it is pinned on both sides: a reader updates by shipping
a new reader, never by anything arriving on the link.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"


@dataclass(frozen=True)
class Metric:
    id: int
    name: str
    unit: str
    scale: int
    delta: bool
    priority: str
    source: str
    # Present only on enum metrics; the human-readable meaning of each value. It
    # never travels on the wire, only into the dashboard's axis labels.
    values: str = ""

    def decode(self, raw: int) -> float | int:
        """Apply the registry's scale. There are no floats on the wire (§4)."""
        return raw if self.scale == 1 else raw / self.scale


@dataclass(frozen=True)
class Registry:
    version: int
    schema_version: int
    max_metrics: int
    metrics: dict[int, Metric]
    retired: frozenset[int]

    def __post_init__(self) -> None:
        assert not (self.retired & set(self.metrics)), "a retired ID was reused"
        always = [m for m in self.metrics.values() if m.priority == "always"]
        assert len(always) <= self.max_metrics // 2, (
            f"always set ({len(always)}) exceeds half of max_metrics; the registry is "
            f"misbuilt and rotating metrics would be starved"
        )

    @property
    def always(self) -> list[Metric]:
        return sorted(
            (m for m in self.metrics.values() if m.priority == "always"),
            key=lambda m: m.id,
        )

    @property
    def rotating(self) -> list[Metric]:
        return sorted(
            (m for m in self.metrics.values() if m.priority == "rotate"),
            key=lambda m: m.id,
        )

    def by_name(self, name: str) -> Metric:
        for metric in self.metrics.values():
            if metric.name == name:
                return metric
        raise KeyError(f"{name} is not in registry v{self.version}")


@cache
def load_registry(path: Path | None = None) -> Registry:
    source = path or (SCHEMA_DIR / "metrics-registry.toml")
    with Path(source).open("rb") as fh:
        raw = tomllib.load(fh)
    return Registry(
        version=raw["registry_version"],
        schema_version=raw["schema_version"],
        max_metrics=raw["max_metrics"],
        metrics={
            int(k): Metric(id=int(k), **v) for k, v in raw["metrics"].items()
        },
        retired=frozenset(int(k) for k in raw.get("retired", {})),
    )
