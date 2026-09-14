"""The population driver (synthetic-population-spec §5).

One instance per live site, inside the per-site sandbox. Deterministic, no LLM, and
no database handle: it acts through the site's own declared forms over HTTP, which
is what stops it producing states the site's interface could not produce and stops
anything that compromises it gaining a write primitive the evaluated agent lacks.
"""

from .driver import (
    MAX_ACTIONS_PER_EPISODE,
    Choreography,
    ContentPool,
    DriverCounters,
    Population,
    PopulationDriver,
)

__all__ = [
    "MAX_ACTIONS_PER_EPISODE",
    "Choreography",
    "ContentPool",
    "DriverCounters",
    "Population",
    "PopulationDriver",
]
