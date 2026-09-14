from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from enum import IntEnum
from pathlib import Path

SITE_ID = re.compile(r"^site-[0-9]{6}$")
HOSTNAME = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}(\.[a-z0-9][a-z0-9\-]{0,62})*$")


class SiteStatus(IntEnum):
    """schemas/status-codes.toml: the two terminal states a revision can hold."""

    LIVE = 40
    RETIRED = 41


@dataclass(frozen=True)
class SiteRecord:
    site_id: str
    revision: int
    bundle_id: str
    hostname: str
    deployment: str  # sandbox path the live app serves from; inside-only
    status: SiteStatus


@dataclass
class RegistryCounters:
    live: int = 0
    retired: int = 0

    def as_metrics(self) -> dict[str, int]:
        return {"sites.live": self.live, "sites.retired": self.retired}


class SiteRegistry:
    """Durable, append-only per revision. A revision is registered once.

    `register` is atomic with respect to `supersedes` (inside-worker SKILL.md §6):
    the hostname moves to the new revision and the old one is retired in the same
    write, so no reader ever sees two live revisions of one site or none.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, SiteRecord] = {}
        if self.path.exists():
            for raw in json.loads(self.path.read_text()):
                raw["status"] = SiteStatus(raw["status"])
                self._records[raw["bundle_id"]] = SiteRecord(**raw)

    @property
    def counters(self) -> RegistryCounters:
        live = sum(1 for r in self._records.values() if r.status is SiteStatus.LIVE)
        return RegistryCounters(live=live, retired=len(self._records) - live)

    def register(
        self,
        *,
        site_id: str,
        revision: int,
        bundle_id: str,
        hostname: str,
        deployment: Path,
        supersedes: str | None = None,
    ) -> SiteRecord:
        assert SITE_ID.match(site_id), site_id
        assert HOSTNAME.match(hostname), hostname
        assert bundle_id not in self._records, f"{bundle_id} already registered; revisions are immutable"
        if supersedes is not None:
            old = self._records.get(supersedes)
            assert old is not None and old.site_id == site_id, (
                f"{bundle_id} supersedes {supersedes}, which is not a registered revision of {site_id}"
            )
        holder = self.live_for_hostname(hostname)
        assert holder is None or holder.bundle_id == supersedes, (
            f"{hostname} is held by {holder.bundle_id if holder else None}, not superseded by {bundle_id}"
        )

        record = SiteRecord(site_id, revision, bundle_id, hostname, str(deployment), SiteStatus.LIVE)
        self._records[bundle_id] = record
        if supersedes is not None:
            self._retire(supersedes)
        self._flush()
        return record

    def retire(self, bundle_id: str) -> SiteRecord:
        assert bundle_id in self._records, bundle_id
        self._retire(bundle_id)
        self._flush()
        return self._records[bundle_id]

    def _retire(self, bundle_id: str) -> None:
        old = self._records[bundle_id]
        self._records[bundle_id] = SiteRecord(
            old.site_id, old.revision, old.bundle_id, old.hostname, old.deployment, SiteStatus.RETIRED
        )

    def get(self, bundle_id: str) -> SiteRecord | None:
        return self._records.get(bundle_id)

    def live_for_hostname(self, hostname: str) -> SiteRecord | None:
        """Inside DNS. One live revision per hostname, or none."""
        live = [r for r in self._records.values() if r.hostname == hostname and r.status is SiteStatus.LIVE]
        assert len(live) <= 1, f"{hostname} has {len(live)} live revisions"
        return live[0] if live else None

    def live_sites(self) -> list[SiteRecord]:
        """What the fake-web search engine mounts."""
        return sorted(
            (r for r in self._records.values() if r.status is SiteStatus.LIVE),
            key=lambda r: r.site_id,
        )

    def _flush(self) -> None:
        payload = [
            {**asdict(r), "status": int(r.status)}
            for r in sorted(self._records.values(), key=lambda r: r.bundle_id)
        ]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        tmp.replace(self.path)
