"""recon-check: the outside local harness for reconstruction agents.

`site-reconstruct` SKILL.md §6. A reconstructor runs this before handing a package
to QA; a package that fails it is not handed on.

The defining property is the asymmetry with go-live. Both deploy the package with
the *same* generator and run the *same* suite, but go-live is inside and returns
enum codes, while recon-check is outside and returns everything: which link was
broken, which row count was wrong, the whole traceback. There is no reason to blind
an agent that is already reading the raw scrape.

Using the same generator is not an optimisation. If recon-check had its own, it
would be testing something other than what ships, and every difference between them
would surface inside as an unexplained code.
"""

from .check import ReconReport, TestOutcome, recon_check

__all__ = ["ReconReport", "TestOutcome", "recon_check"]
