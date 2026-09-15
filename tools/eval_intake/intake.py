from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from tools.bundle_lint import lint_choreography
from tools.golive import GoLiveService
from tools.registry import SiteRegistry


class IntakeStatus(IntEnum):
    FILED = 30
    UNSUPPORTED = 21
    DECRYPT_FAIL = 32
    SITE_NOT_LIVE = 53


@dataclass(frozen=True)
class IntakeEmission:
    bundle_id: str
    status_code: IntakeStatus


class EvalDefinitions:
    """question_id -> the sandbox holding its choreography and decrypted pools."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._by_question: dict[str, str] = json.loads(self.path.read_text()) if self.path.exists() else {}

    def file(self, question_id: str, sandbox: Path) -> None:
        self._by_question[question_id] = str(sandbox)  # a newer revision replaces the older
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._by_question, indent=1, sort_keys=True))

    def sandbox_for(self, question_id: str) -> Path | None:
        value = self._by_question.get(question_id)
        return Path(value) if value else None

    def __len__(self) -> int:
        return len(self._by_question)


class EvalIntake:
    def __init__(self, inbox: Path, golive: GoLiveService, registry: SiteRegistry, definitions: EvalDefinitions) -> None:
        self.inbox = Path(inbox)
        self._golive = golive
        self._registry = registry
        self.definitions = definitions
        self.emissions: list[IntakeEmission] = []

    def pending(self) -> list[str]:
        entries = []
        for path in self.inbox.iterdir():
            if path.is_dir() and (path / "manifest.json").is_file():
                entries.append((json.loads((path / "manifest.json").read_text())["sequence"], path.name))
        return [name for _, name in sorted(entries)]

    def run_once(self) -> IntakeEmission | None:
        queue = self.pending()
        return self.process(queue[0]) if queue else None

    def process(self, bundle_id: str) -> IntakeEmission:
        source = self.inbox / bundle_id
        try:
            emission = self._process(bundle_id, source)
        finally:
            shutil.rmtree(source, ignore_errors=True)
        self.emissions.append(emission)
        return emission

    def _process(self, bundle_id: str, source: Path) -> IntakeEmission:
        doc = json.loads((source / "spec" / "choreography.json").read_text())
        live = next((r for r in self._registry.live_sites() if r.site_id == doc["site_id"]), None)
        if live is None:
            return IntakeEmission(bundle_id, IntakeStatus.SITE_NOT_LIVE)
        spec = json.loads((Path(live.deployment) / "spec" / "site.json").read_text())
        if lint_choreography(doc, spec):
            return IntakeEmission(bundle_id, IntakeStatus.UNSUPPORTED)

        result = self._golive.unseal_eval(source)
        if int(result.status) != IntakeStatus.FILED:
            return IntakeEmission(bundle_id, IntakeStatus.DECRYPT_FAIL)
        self.definitions.file(doc["question_id"], self._golive.sandbox_root / bundle_id)
        return IntakeEmission(bundle_id, IntakeStatus.FILED)
