"""bundle-lint: the structure/content tripwire.

Runs in two places with identical rules (bundle-format-spec §6, §8.1.5, §8.2.5):
outside before signing, and on the receiver after signature verification. Findings
are enum codes and JSON-pointer locations. There is no prose output, because a
finding is reported across the diode as status 14 plus codes, and a string that
could carry content would defeat the point.
"""

from .findings import Finding
from .lint import lint_bundle, lint_spec, lint_suite

__all__ = ["Finding", "lint_bundle", "lint_spec", "lint_suite"]
