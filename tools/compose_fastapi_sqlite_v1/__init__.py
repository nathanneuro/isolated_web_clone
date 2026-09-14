"""The Tier A deterministic generator: site spec -> running FastAPI+SQLite app.

There is no LLM in this module and there must never be one. The inside worker's job
(inside-worker SKILL.md) is to choose among the patterns this generator supports and
to fail closed when the spec needs one it does not; the generator is what turns that
choice into code. Keeping generation deterministic is what makes the go-live test
suite meaningful -- the harness knows what correct looks like because the shapes are
fixed.
"""

from .compose import WRITER_COLUMN, WRITER_HEADER, ComposeError, compose_app

__all__ = ["WRITER_COLUMN", "WRITER_HEADER", "ComposeError", "compose_app"]
