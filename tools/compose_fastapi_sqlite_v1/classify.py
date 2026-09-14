"""Dry-run classification: does every element of a spec map to a supported pattern?

This is what the worker calls before composing (inside-worker SKILL.md step 2). It
reads structure only and builds nothing. The answer is a set of element ids, never
prose, because the worker forwards it as a status code and a human reads the ids
at the terminal.

There are currently no FLAGGED patterns: every element is either supported by the
one implementation of it or unsupported. When the generator grows a second way to
build something, that choice appears here as a pattern id, and the worker's chooser
hook is where it gets made.
"""

from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_FRAMEWORK = "fastapi-sqlite-v1"
SUPPORTED_MUTATION_OPS = frozenset({"insert", "update", "delete"})
SUPPORTED_ENGINES = frozenset({"jinja2"})
SUPPORTED_SEARCH_KINDS = frozenset({"bm25"})
# "none" and "dead_end" render as a fixed page; anything functional is not built.
SUPPORTED_INTERACTIONS = frozenset({"none", "dead_end"})


@dataclass(frozen=True)
class Classification:
    unsupported: tuple[str, ...] = ()
    flagged: tuple[str, ...] = ()  # pattern ids needing a choice; none exist yet

    @property
    def supported(self) -> bool:
        return not self.unsupported and not self.flagged


def classify_spec(spec: dict) -> Classification:
    """Element ids the generator cannot build, in document order."""
    unsupported: list[str] = []
    if spec.get("framework") != SUPPORTED_FRAMEWORK:
        unsupported.append("framework")

    for route in spec.get("routes", []):
        if "redirect_to" in route:
            unsupported.append(route["id"])
        elif route["method"] == "GET" and "template" not in route:
            unsupported.append(route["id"])
        elif route["method"] == "POST" and "mutation" not in route:
            unsupported.append(route["id"])
        elif route["method"] not in ("GET", "POST"):
            unsupported.append(route["id"])

    for template in spec.get("templates", []):
        if template.get("engine") not in SUPPORTED_ENGINES:
            unsupported.append(template["id"])

    for query in spec.get("queries", []):
        if "join" in query:
            unsupported.append(query["id"])

    for mutation in spec.get("mutations", []):
        if mutation.get("op") not in SUPPORTED_MUTATION_OPS:
            unsupported.append(mutation["id"])
        elif mutation["op"] in ("update", "delete") and not mutation.get("bind"):
            unsupported.append(mutation["id"])

    for search in spec.get("search", []):
        if search.get("kind") not in SUPPORTED_SEARCH_KINDS or len(search.get("shard_refs", [])) != 1:
            unsupported.append(search["id"])

    for name, value in (spec.get("interactions") or {}).items():
        if value not in SUPPORTED_INTERACTIONS:
            unsupported.append(f"interactions.{name}")

    return Classification(unsupported=tuple(unsupported))
