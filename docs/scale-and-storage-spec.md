# Airgapped Web Environment — Scale and Storage

**Version:** 0.1 (draft)
**Date:** 2026-09-14
**Companion to:** Design Plan v0.1, Bundle Format Specification v0.1, Agent Sandbox v0.1

---

## 1. The target scale

This design is sized for **hundreds of thousands of reconstructed sites**, produced by
a crawl-and-reconstruction effort costing millions of dollars, serving evaluation of
models capable enough that "the model broke out of the eval harness" is a realistic
incident rather than a thought experiment.

That scale is not a detail. It changes several answers that would go the other way at
the scale of WebArena's six hand-built sites, and it is worth being explicit about
which ones, because a reader who mentally substitutes "a few dozen sites" will
reasonably conclude that parts of this design are over-built, and a reader who
substitutes "the whole web" will conclude they are under-built.

Concretely, assume:

| Quantity | Order of magnitude |
|----------|--------------------|
| Reconstructed sites | 10^5 – 10^6 |
| Sites live simultaneously | 10^2 – 10^4 (instantiated per episode, not all at once) |
| Total content | 10^12 – 10^13 bytes |
| Bundles crossing ingress | one per site revision; 10^5+ on initial fill |
| Episodes per eval run | 10^3 – 10^6 |
| Per-episode environment reset | must be cheap enough to be uninteresting |

---

## 2. Where the site data lives

This section exists because the obvious answer is wrong at this scale, and the
obvious answer is the one most reviewers will arrive at first.

### 2.1 The proposal: one real database behind all the mocked sites

The instinct is sound. A real RDBMS gives proper types, transactions, concurrency,
one place to administer and back up, and one place for a human at the wired terminal
to run a query when something looks off. WebArena does exactly this, with real
Postgres and MySQL behind its replicas, and it works well.

It works well **because WebArena has six sites**.

### 2.2 Why it inverts at 10^5 sites

**Operationally.** Postgres is not built for 100,000 databases in a cluster, nor for
100,000 schemas in a database. Catalog size, autovacuum scheduling, connection
management, and backup granularity all degrade well before that. The practical
ceiling is low thousands. Per-episode reset is the sharper problem: `CREATE DATABASE
… TEMPLATE` requires no other connections to the template and serialises, so a reset
that must happen between every one of 10^6 episodes becomes the bottleneck of the
entire eval.

**Architecturally, and this is the part that matters here.** A shared database server
is a *shared component that every mocked site's application connects to*. The sites
are LLM-generated applications, built at scale by cheap agents, from scraped content
chosen by nobody in particular. That is precisely the population of code you should
assume contains injection bugs.

The entire premise of this design is that the agent under evaluation is an effective
optimiser that will find such bugs. Giving every generated site a connection to one
process tree that also holds every other site's data creates a lateral path from "the
agent found an injection in one mocked forum" to "the agent can read and write the
backing state of every site in the corpus," including sites whose state is the ground
truth for its own reward. Role and grant separation per site reduces this; it does not
remove the shared attack surface, and it is a lot of correctness to get exactly right
100,000 times.

For a system whose reason to exist is eliminating shared paths, adding one at the
centre of the environment zone is the wrong direction.

### 2.3 What this design does instead

**One SQLite file per site.** Not because SQLite is better software than Postgres,
but because the properties that matter here are the ones a file has and a server does
not:

- **Reset is a file operation.** A copy, or on XFS or btrfs a reflink clone that is
  effectively free. At 10^6 episodes this is the difference between reset being
  invisible and reset being the eval's critical path. This is the AppWorld pattern
  (per-task DB copies), chosen there for the same reason.
- **Isolation is structural.** A site's app can open its own file and has no handle
  to any other. There is no shared process, no shared connection pool, no shared
  catalog, and no grant matrix to get right 100,000 times. A compromised site app
  reaches one site's data, which is the blast radius the design wants.
- **It scales flat.** 10^6 files is unremarkable. 10^6 databases is not.
- **In-process rollouts stay free.** The design plan's serverless-first execution
  (FastAPI in-process, no server hop) requires an embedded store to be worth doing.

The concurrency argument for a server does not apply: each episode is one agent
against one site, so there is a single writer by construction.

### 2.4 Where a real database does belong

Nathan's instinct is right about the *existence* of a real database inside; it is the
placement that changes. There is a body of genuinely relational, genuinely concurrent,
genuinely ad-hoc-queried data inside the airgap, and it is not the page content:

**Postgres runs the inside control plane.** One cluster, administered normally,
holding:

| Data | Why relational |
|------|----------------|
| Site registry: 10^5+ rows of site_id, revision, tier, hostname, live/retired, go-live status | queried constantly by the worker, the search engine, and humans |
| Bundle receipts and sequence high-water marks | transactional; must survive a crash mid-receive |
| Eval run metadata, episode records, per-test results | the thing a human at the terminal actually wants to slice |
| Escape watchdog detector state and halt history | must be durable and must not live on a host the agent can touch |
| Cross-site search index metadata | which shard serves which site |

**And nothing in the agent zone or any site app can reach it.** The control plane is a
separate zone from the environment zone, on the far side of the same kind of boundary
the sandbox spec draws around the inference server. A site's app has a file. The
control plane has a database. They do not meet.

That split is the actual recommendation: **a real database for the things a database
is for, and files for the things that need to be cloned a million times and isolated
from each other.**

### 2.5 If you want Postgres for site backends anyway

Some deployments will want server-backed sites for a small Tier A subset — sites where
the interaction genuinely needs transactions across tables, or where an eval targets
database-backed behaviour specifically. The bundle format already supports this
without change: `framework` is a versioned enum, and `fastapi-postgres-v1` can join
`fastapi-sqlite-v1` as a Tier A target with its own deterministic generator.

If you do that, two rules are not optional: a dedicated role per site with grants on
its own schema and no other, and that subset kept small enough that the per-episode
reset cost and the shared-surface argument in §2.2 both stay tolerable. It is a
carve-out for the sites that need it, not the default for the corpus.

---

## 3. Other things 10^5 sites change

**Shared-asset deduplication stops being a nicety.** Bundle spec §11.4 lists
re-encrypting a common CSS framework per bundle as "wasteful but keeps the key
hierarchy flat." At 10^5 bundles it is no longer a rounding error, and a shared-asset
bundle type with its own key becomes worth the added hierarchy. Flagging it here so
the open question is costed against the real number.

**Composition must be overwhelmingly deterministic.** Bundle spec §11.2 asks how much
of the long tail a no-LLM composer can handle. At this scale the answer has to be
"nearly all of it": an LLM in the composition path for 10^5 sites is both a cost
problem and a reliability problem, and the worker's LLM has to be an exception handler
for the residue. Budget the generator's pattern coverage accordingly, and treat
`UNSUPPORTED` rates as a headline metric rather than an error log.

**Per-site search and fake-web search are different systems.** A per-site BM25 shard
shipped in the bundle serves that site's own search box. The fake-web engine the agent
uses to *find* sites needs a cross-site index over 10^5 corpora, built inside from the
registry, and it is the one component that legitimately sees across sites. It should
therefore be treated as a trusted component with its own zone, not as a site.

**Ingress is a fill problem before it is a steady-state problem.** 10^5 bundles on
initial fill, at a diode's real throughput, is measured in days. Sequence numbers are
int64 and fine; the receiver's per-bundle verification cost is what to profile, since
it hashes every byte that enters. Plan the fill as an operation with a schedule, not
as something that happens while you wait.

**Storage is unremarkable and the reset strategy is not.** 10^12–10^13 bytes is
ordinary. The thing to design deliberately is the filesystem: reflink-capable (XFS or
btrfs) turns per-episode reset from a copy into a metadata operation, and that choice
is worth more to eval throughput than anything else in this document.

---

## 4. Open questions

1. **Reflink availability inside.** The reset argument in §2.3 leans on copy-on-write
   clones. Worth confirming the inside storage stack supports them before committing
   to per-episode reset at 10^6 scale; the fallback is a page-cache-warm copy, which
   is slower but still beats `CREATE DATABASE`.
2. **Control-plane Postgres as a single point of failure.** It is not on the agent's
   path, so it is not a security concern, but an eval run that cannot record results
   is a wasted run. Replication inside, or accept the outage and re-run.
3. **Registry queries from the worker.** The worker needs to read the site registry to
   retire superseded revisions. That is a read path from a low-trust component into
   the control plane and it should be a narrow, fixed-query interface rather than SQL
   access, for the same reasons the brokers exist.
4. **When does the Tier A Postgres carve-out become the majority?** If evals move
   toward database-heavy interactions, §2.5's small subset stops being small and §2.2's
   analysis needs redoing rather than reused.
