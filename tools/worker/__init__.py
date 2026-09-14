"""The inside worker (bundle-format-spec §8.4, skills/inside-worker/SKILL.md).

The one LLM-adjacent component inside the trusted zone, and the reason it is safe
to run is what it cannot see. It reads `manifest.json`, `spec/site.json`, and
`tests/suite.json`. The blobs under `content/` and `index/` pass through its hands
as ciphertext it has no key for, and every decision it makes is a choice among the
generator's enumerated patterns, made from structure alone.

In this reference implementation the worker is deterministic end to end, because
the generator has no FLAGGED patterns yet: every element is either supported or
not. The `PatternChooser` hook is where a model would sit when that changes, and
what it would receive is a pattern id and an option enum, never a byte of content.

Its outputs are status codes. It has no channel for explaining anything, and that
is the design: a confusing bundle is a failure code and the next bundle.
"""

from .worker import (
    MAX_ATTEMPTS,
    PatternChooser,
    StatusEmission,
    Worker,
    WorkerCounters,
    WorkerStatus,
)

__all__ = [
    "MAX_ATTEMPTS",
    "PatternChooser",
    "StatusEmission",
    "Worker",
    "WorkerCounters",
    "WorkerStatus",
]
