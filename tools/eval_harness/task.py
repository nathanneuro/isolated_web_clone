"""Build an Inspect Task over a go-live'd site, with per-episode reset."""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample

from tools.brokers import BrokerGate, EnvBroker
from tools.compose_fastapi_sqlite_v1 import compose_app

from .question import EvalQuestion
from .counters import EvalCounters
from .scorer import reward_scorer, state_diff_scorer
from .solver import broker_web_agent


@dataclass(frozen=True)
class Episode:
    env: EnvBroker
    db_path: Path


class SiteEnvFactory:
    """Hands out a fresh site per episode.

    Per-episode reset is a file copy of the seed DB (scale-and-storage-spec §2.3).
    On a reflink-capable filesystem this is a metadata operation; here it is an
    ordinary copy, which is still far cheaper than a server-side database reset and
    is why site backends are files.

    Episodes are keyed by an id the caller supplies, not by the question: Inspect
    runs epochs of the same question concurrently, and two episodes sharing a
    database file would reset each other under the scorer.
    """

    def __init__(
        self,
        spec: dict,
        content_dir: Path,
        seed_db: Path,
        work_dir: Path,
        gate: BrokerGate | None = None,
    ) -> None:
        self.spec = spec
        self.content_dir = Path(content_dir)
        self.seed_db = Path(seed_db)
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        # Shared with every EnvBroker this factory hands out. The watchdog severs
        # it; after that no episode opens and the open one is refused.
        self.gate = gate or BrokerGate()
        self.open_episodes: list[Episode] = []  # read by the D10 detector

    def db_path_for(self, episode_id: str) -> Path:
        assert episode_id and "/" not in episode_id, episode_id
        return self.work_dir / f"{episode_id}.sqlite"

    @contextmanager
    def episode(self, question: EvalQuestion, episode_id: str) -> Iterator[Episode]:
        """A fresh site for one episode. The database outlives the site so the
        scorer can read it; the client does not."""
        from fastapi.testclient import TestClient

        assert question.site_id == self.spec["site_id"], (
            f"{question.id} is for {question.site_id}; this factory serves {self.spec['site_id']}"
        )
        assert not self.gate.severed, "environment zone is severed; the run is halted"
        db_path = self.db_path_for(episode_id)
        assert not db_path.exists(), f"episode {episode_id} already ran"
        shutil.copyfile(self.seed_db, db_path)
        site = compose_app(self.spec, self.content_dir, db_path)
        with TestClient(site.app, base_url=f"http://{site.hostname}") as client:
            episode = Episode(EnvBroker(client, site.hostname, gate=self.gate), db_path)
            self.open_episodes.append(episode)
            try:
                yield episode
            finally:
                self.open_episodes.remove(episode)


def build_task(
    questions: list[EvalQuestion],
    action_broker,
    env_factory: SiteEnvFactory,
    name: str = "isolated-web-clone",
    emitter=None,
    counters: EvalCounters | None = None,
) -> Task:
    by_id = {q.id: q for q in questions}
    assert len(by_id) == len(questions), "duplicate question ids"
    dataset = MemoryDataset(
        [Sample(id=q.id, input=q.task, target=q.expected_answer or "") for q in questions],
        name=name,
    )
    return Task(
        dataset=dataset,
        solver=broker_web_agent(action_broker, env_factory, by_id, emitter, counters),
        scorer=[state_diff_scorer(by_id), reward_scorer(by_id, counters)],
        name=name,
    )
