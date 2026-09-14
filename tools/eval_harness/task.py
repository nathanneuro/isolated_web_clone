"""Build an Inspect Task over a go-live'd site, with per-episode reset."""

from __future__ import annotations

import shutil
from pathlib import Path

from inspect_ai import Task
from inspect_ai.dataset import MemoryDataset, Sample

from tools.brokers import EnvBroker
from tools.compose_fastapi_sqlite_v1 import compose_app

from .question import EvalQuestion
from .scorer import state_diff_scorer
from .solver import broker_web_agent


class SiteEnvFactory:
    """Hands out a fresh site per episode.

    Per-episode reset is a file copy of the seed DB (scale-and-storage-spec §2.3).
    On a reflink-capable filesystem this is a metadata operation; here it is an
    ordinary copy, which is still far cheaper than a server-side database reset and
    is why site backends are files.
    """

    def __init__(self, spec: dict, content_dir: Path, seed_db: Path, work_dir: Path) -> None:
        self.spec = spec
        self.content_dir = Path(content_dir)
        self.seed_db = Path(seed_db)
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def db_path_for(self, question: EvalQuestion) -> Path:
        return self.work_dir / f"{question.id}.sqlite"

    def __call__(self, question: EvalQuestion) -> EnvBroker:
        from fastapi.testclient import TestClient

        episode_db = self.db_path_for(question)
        shutil.copyfile(self.seed_db, episode_db)
        site = compose_app(self.spec, self.content_dir, episode_db)
        client = TestClient(site.app, base_url=f"http://{site.hostname}")
        client.__enter__()
        return EnvBroker(client, site.hostname)


def build_task(
    questions: list[EvalQuestion],
    action_broker,
    env_factory: SiteEnvFactory,
    name: str = "isolated-web-clone",
) -> Task:
    by_id = {q.id: q for q in questions}
    dataset = MemoryDataset(
        [
            Sample(id=q.id, input=q.task, target=q.expected_answer or "state-diff")
            for q in questions
        ],
        name=name,
    )
    return Task(
        dataset=dataset,
        solver=broker_web_agent(action_broker, env_factory, by_id),
        scorer=state_diff_scorer(by_id),
        name=name,
    )
