"""recon-check: the outside agent's local harness (site-reconstruct SKILL.md §6).

Runs on a reconstructor's package before hand-off: bundle-lint on the spec and
suite, the same generator the inside worker uses, and the same suite the go-live
service runs. The difference from inside is what comes back. Outside there is no
diode and no LLM downstream, so a failure returns the test's name, its kind, and
the response the runner saw, not a code. That is the point of running it here:
the reconstructor gets logs, the inside worker gets integers.

Nothing here encrypts anything. The package is plaintext and stays that way.
"""

from .check import CheckReport, TestOutcome, check_package

__all__ = ["CheckReport", "TestOutcome", "check_package"]
