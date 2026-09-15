from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from tools.compose_fastapi_sqlite_v1.compose import Bm25Shard
from tools.registry import SiteRegistry

PARAM = re.compile(r"\{([a-z][a-z0-9_]*)\}")


@dataclass(frozen=True)
class Hit:
    site_id: str
    hostname: str
    doc_id: int
    title: str
    path: str  # the route on that site that renders the document
    score: float


@dataclass
class SearchCounters:
    indexed_sites: int = 0
    queries: int = 0
    _window: list[float] = field(default_factory=list)

    def as_metrics(self) -> dict[str, int]:
        now = self._window[-1] if self._window else 0.0
        recent = [t for t in self._window if now - t < 60]
        return {"search.indexed_sites": self.indexed_sites, "search.qps": int(round(len(recent) / 60))}


@dataclass(frozen=True)
class _Mount:
    site_id: str
    hostname: str
    shard: Bm25Shard
    detail_path: str  # e.g. /thread/{thread_id}


class FakeWebSearch:
    """Mounts live sites from the registry; answers queries across all of them."""

    def __init__(self, registry: SiteRegistry, *, clock: Callable[[], float] = time.time) -> None:
        self._registry = registry
        self._clock = clock
        self._mounts: dict[str, _Mount] = {}
        self.counters = SearchCounters()

    def refresh(self) -> int:
        """Re-read the registry. Sites that went live are mounted; retired ones dropped."""
        live = {r.bundle_id: r for r in self._registry.live_sites()}
        for bundle_id in list(self._mounts):
            if bundle_id not in live:
                del self._mounts[bundle_id]
        for bundle_id, record in live.items():
            if bundle_id not in self._mounts:
                self._mounts[bundle_id] = self._mount(record.site_id, record.hostname, Path(record.deployment))
        self.counters.indexed_sites = len(self._mounts)
        return len(self._mounts)

    @staticmethod
    def _mount(site_id: str, hostname: str, sandbox: Path) -> _Mount:
        # The serving sandbox holds the decrypted shard and the plaintext spec. The
        # engine reads structure to learn where the shard is and which route shows
        # a document; it does not read the site's database.
        spec = json.loads((sandbox / "spec" / "site.json").read_text())
        search = spec["search"][0]
        shard = Bm25Shard(sandbox / search["shard_refs"][0])
        table = search["tables"][0]
        by_id = {q["id"]: q for q in spec.get("queries", [])}
        detail = next(
            (route["path"] for route in spec["routes"]
             if route["method"] == "GET" and any(
                 by_id[q]["table"] == table and "id" in (by_id[q].get("where") or {})
                 for q in route.get("queries", []))),
            None,
        )
        assert detail is not None, f"{site_id}: no route renders a single {table} row"
        return _Mount(site_id, hostname, shard, detail)

    def search(self, query: str, limit: int = 10) -> list[Hit]:
        self.counters.queries += 1
        self.counters._window.append(self._clock())
        hits: list[Hit] = []
        for mount in self._mounts.values():
            for result in mount.shard.search(query, limit=limit):
                hits.append(Hit(
                    mount.site_id, mount.hostname, result["id"], result["title"],
                    PARAM.sub(str(result["id"]), mount.detail_path), result["score"],
                ))
        hits.sort(key=lambda h: (-h.score, h.site_id, h.doc_id))
        return hits[:limit]

    @property
    def sites(self) -> list[str]:
        return sorted(m.site_id for m in self._mounts.values())
