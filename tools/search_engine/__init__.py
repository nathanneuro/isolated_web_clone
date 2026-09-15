"""The fake-web search engine: how an agent finds a site it has not been told about.

Per-site search and fake-web search are different systems (scale-and-storage-spec
§3). A site's own BM25 shard serves its search box. This engine mounts every live
site's shard from the registry and answers `search(query)` across all of them,
which makes it the one component that legitimately sees across sites. It is a
trusted component with its own zone: no LLM, no database handle, no write path to
any site, and it reads shards from the serving sandboxes it is pointed at, never
from the bundles.

Served as a tool, Search-R1 style: a query in, ranked hits out, each hit a site,
a document, and the path that renders it.
"""

from .engine import FakeWebSearch, Hit, SearchCounters

__all__ = ["FakeWebSearch", "Hit", "SearchCounters"]
