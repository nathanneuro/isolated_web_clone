"""The site registry: which revision of which site is live, under which hostname.

This is the one piece of the inside control plane (scale-and-storage-spec §2.4)
the reference implementation carries. It is a JSON file here and a Postgres table in
a deployment; what matters is who may write it. The worker submits structured
registration requests, the command executor applies `set_live` and `retire`, and
nothing in the agent zone or any site app can reach it. The env broker resolves a
hostname to a live deployment through it, which is what "the agent cannot reach a
hostname that is not a registered live site" means concretely.
"""

from .registry import RegistryCounters, SiteRecord, SiteRegistry, SiteStatus

__all__ = ["RegistryCounters", "SiteRecord", "SiteRegistry", "SiteStatus"]
