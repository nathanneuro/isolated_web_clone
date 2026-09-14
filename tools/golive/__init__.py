"""The go-live service: the only thing inside that holds the content key.

Deterministic, no LLM, small enough to review in full (bundle-format-spec §8.5).
It decrypts into a serving sandbox, runs the suite, and returns codes. It never
returns output, diffs, screenshots, or decrypted anything to the worker.
"""

from .service import GoLiveResult, GoLiveService, TestResult
from .runner import run_suite

__all__ = ["GoLiveResult", "GoLiveService", "TestResult", "run_suite"]
