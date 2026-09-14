# Offline Web Environments for Agent Training: Source Map and Design Plan

**Date:** 2026-09-14
**Purpose:** Survey of existing work on offline/"fake internet" environments for web-agent training, and a design plan for the novel component: LLM-regenerated functional website replicas with an integrated local search engine.

> **Scope note.** This document describes the *environment*, including a crawl tier
> that touches the real internet. The security architecture that wraps it — one-way
> ingress and egress, content the inside worker cannot read, numeric-only outbound —
> is in [the README](../README.md) and [`bundle-format-spec.md`](bundle-format-spec.md).
> Per that split, **this repository contains no scraped data, and the scraper and
> explorer of §3.1 steps 0–1 are deliberately absent.** Everything published here runs
> on the invented example site. If you are implementing the crawl tier, the robots,
> terms, and rate-limit judgement in §3.5 is yours to make per deployment.

---

## 1. Background

The problem: training web agents requires interacting with the real web, which brings rate limits, anti-bot measures, non-determinism, and irreversible side effects (posting, purchasing, deleting). The field has converged on two partial solutions:

- **Static captures** — scrape sites, serve frozen snapshots locally (scale, no function).
- **Functional replicas** — rebuild working sites on internal servers (function, tiny scale).

The plan this document supports is the synthesis: crawl content, use an LLM-agent pipeline to rebuild working-but-not-identical versions of the scraped sites (with seeded backends), and serve them behind a local working search engine.

---

## 2. Source Map

### 2.1 Offline web environments ("fake internet")

| # | Source | What it is | Why it's relevant |
|---|--------|------------|-------------------|
| 1 | **InSTA: Towards Internet-Scale Training For Agents** — Trabucco et al., CMU/Amazon, Feb 2025. [arXiv:2502.06776](https://arxiv.org/abs/2502.06776). Repo: [data-for-agents/insta](https://github.com/data-for-agents/insta) | Pipeline that crawls 150k websites, captures snapshots, serves them locally so agents train on a static offline copy of the web. Fully automatic task generation: LLM proposes tasks per site → agents execute → LLM judge filters trajectories. Released the InSTA-150k dataset and a trained 72B model that beats GPT-4o/Claude-3.5 on WebArena and OSWorld. | The closest existing system to the crawl tier of our plan. Proves (a) offline captured web can be served at scale, (b) agents trained on static snapshots still transfer to live functional sites, (c) LLM-judge filtering enables millions of tasks. Its weaknesses — no search, no functional backends, noisy labels — are exactly what our plan adds. |
| 2 | **WebArena** — Zhou et al., CMU, ICLR 2024. [arXiv:2307.13854](https://arxiv.org/abs/2307.13854) | Self-contained environment of functional replicas: GitLab, Magento, Reddit-clone (Postmill), OpenStreetMap, WordPress CMS. 812 human-written tasks, 241 intent templates, programmatic reward functions that verify real environment state. | The gold standard for functional fidelity and verifiable rewards. Only 6 sites because everything was hand-built — demonstrates both the value of functional replicas and the cost barrier our LLM pipeline is meant to remove. Agents never touch the real web. |
| 3 | **WebArena-Infinity** — Zhou et al., 2025 | Scale-up of WebArena: self-contained static HTML/CSS/JS environments, browser-owned state pushed to a lightweight server, fast resets, LLM-generated but verifiable tasks. Suited for RL training. | Evidence the two lines are converging: WebArena-style verification applied to static, resettable, RL-oriented environments. Direct prior art for our "thin functional layer over static sites" tier. |
| 4 | **TimeWarp: Evaluating Web Agents by Revisiting the Past** — 2026. [arXiv:2603.04949](https://arxiv.org/abs/2603.04949) | Evaluation environment with locally hosted Flask backends serving frozen web data (news, shopping, wiki) from past dates; agents can alter the past. | Shows the locally-hosted-fake-web pattern applied to temporal evaluation; useful reference for serving infrastructure. |
| 5 | **WebShop** — Yao et al., Princeton, NeurIPS 2022 | Simulated e-commerce site built from 1.18M scraped Amazon products, with functional search and reward functions; RL-trained agents outperform imitation learning. | Early proof that a mock site with a working internal search engine over scraped data supports effective RL. The earliest instance of "scraped corpus + functional mock + training" in this lineage. |

### 2.2 Local/mock search engines for training

| # | Source | What it is | Why it's relevant |
|---|--------|------------|-------------------|
| 6 | **Search-R1** — Jin et al., UIUC, Mar 2025. [arXiv:2503.09516](https://arxiv.org/abs/2503.09516). Repo: [PeterJinGo/search-r1](https://github.com/PeterJinGo/search-r1) | Open-source RL framework (veRL-based) with a pluggable search engine: local BM25, local dense retriever (GPU flat / CPU ANN), or online APIs. The LLM calls a local retrieval server (`http://127.0.0.1:8000/retrieve`) as its search tool; token-masking of retrieved docs stabilizes training. | The reference implementation for our local search tier. "Retriever-as-search-tool behind localhost" is a solved pattern with code to copy. Wikipedia is the example corpus; we swap in our crawl index. |
| 7 | **R1-Searcher** — RUC, Mar 2025. [arXiv:2503.05592](https://arxiv.org/abs/2503.05592) | Incentivizes search in reasoning models via RL against local Wikipedia retrieval (KILT corpus). Key result: the locally-trained model transfers to online Google search, which further improves results. | Answers the critical sim-to-real question for our mock search engine: training against a local index teaches a skill that survives contact with a real engine. Justifies building the search tier offline first. |
| 8 | **Tongyi DeepResearch** — Alibaba, Sept 2025. [arXiv:2510.24701](https://arxiv.org/abs/2510.24701) | Explicit three-tier training environments: prior-world (LLM simulates everything), simulated (offline environment on the 2024 Wikipedia database + local RAG tools simulating the web), and real-world. GRPO runs in the simulated env whose reward curve tracks the real one — they call it a "wind tunnel laboratory." For the real tier, a unified tool sandbox adds QPS limits, result caching, timeout-retry, failover to backup search APIs to make the real API deterministic. | The most complete worked example of building an offline search-backed environment for RL, plus the best articulation of why (API volatility corrupts RL trajectories). Their simulated-env-first, real-env-validation methodology is our validation template. |
| 9 | **Awesome Search Agent Papers** — EMNLP 2025 tutorial repo ([sunnweiwei/search-agent](https://github.com/sunnweiwei/search-agent)) | Curated survey of 50 search-agent papers (training data, benchmarks, agent frameworks, web agents, RL environments). | Map of the surrounding field; useful for positioning and for mining additional related work. |

### 2.3 Stateful mock backends (API-level mock worlds)

| # | Source | What it is | Why it's relevant |
|---|--------|------------|-------------------|
| 10 | **AppWorld** — Stony Brook/AI2/Saarland, ACL 2024 Best Resource. [arXiv:2407.18901](https://arxiv.org/abs/2407.18901) | Mock ecosystem of 9 day-to-day apps (Spotify/Gmail/Amazon-like) with 457 APIs and a DB of 101 tables/370k rows simulating 100 users. State-based unit tests, including collateral-damage checks for things the agent shouldn't have touched. Per-task DB copies give exact resettable state; agents can hit the FastAPI app in-process ("serverless") or served over Docker. Ships train splits explicitly for training agents. | The gold-standard reference implementation for stateful mock backends. Four design choices to copy directly: state-based verification, collateral-damage penalties, per-episode DB snapshots, in-process execution for cheap high-throughput rollouts. |
| 11 | **τ-bench** — Sierra, 2024. [arXiv:2406.12045](https://arxiv.org/abs/2406.12045) | Mock retail/airline domains (1,000 orders / 2,000 reservations). Construction in 3 stages: humans design schema + APIs + policy doc → GPT-4 generates data entries at scale → humans write scenarios + target goal states. Agent talks to an LLM user simulator; success judged on final DB state vs. annotated goal, never the conversation; pass^k for reliability. | The construction recipe for our backend-generation tier: human-designed (or LLM-proposed) schema, LLM-populated data, state-diff verification. Their Stage-2 pattern is exactly our setting (seed from extracted content). |
| 12 | **ToolSandbox** — Apple, 2024. [arXiv:2408.11401](https://arxiv.org/abs/2408.11401) | Python-native mock environment; tools execute in a live interpreter and mutate world state with implicit state dependencies (can't search restaurants while cellular is off). Evaluation via Milestones (DAG of required states → intermediate rewards) and Minefields (events that must not occur → zero score, penalizing hallucination). Validated LLM user simulator with "knowledge boundary" prompting. | The reward-engineering reference. Milestones give intermediate reward for long-horizon RL; minefields counter reward hacking. State-dependency modeling is a checklist for making our regenerated backends behave realistically. |
| 13 | **Klavis Sandbox-as-a-Service** — klavis.ai (commercial) | Ephemeral instances of real services (Google Calendar, Salesforce, Slack) seeded via JSON world-state, interacted with via MCP, with seed → interact → dump → reset lifecycle, marketed for RL training (dump = reward via state diff). | Productized existence proof of the convergent mock-backend lifecycle. Also a fallback option: rent stateful backends for the slice of tasks that need real services rather than regenerated ones. |

### 2.4 Testing-grade mocks (for our own CI, not the training env)

| # | Source | What it is | Why it's relevant |
|---|--------|------------|-------------------|
| 14 | **MockServer** — mock-server.com | General-purpose mock HTTP server; now mocks LLM provider APIs byte-for-byte including streaming SSE, tool calls, multi-turn via scenario state, chaos/fault injection. | Mock the LLM APIs when unit-testing our environment's determinism — otherwise env bugs are indistinguishable from model flakiness. |
| 15 | **AIMock / LLMock / MCPMock / VectorMock** — CopilotKit ([CopilotKit/AIMock](https://github.com/CopilotKit/AIMock)) | One config mocks the whole agentic stack: 11 LLM providers, a mock MCP server, a mock vector DB for deterministic RAG retrieval, plus search/rerank/moderation services; record/replay of real API responses. | Same purpose as #14 with the search/retrieval mocking included — handy for testing our search-tool integration deterministically. |

### 2.5 Isolation infrastructure (hosting tier)

| # | Source | What it is | Why it's relevant |
|---|--------|------------|-------------------|
| 16 | **Modal** — modal.com | Firecracker-based sandboxes; 100k+ concurrent, fast startup, checkpoint/snapshot support. | Candidate substrate for hosting thousands of parallel, resettable per-agent replicas during RL rollouts. Alternatives: E2B, Runloop, Daytona. |

### 2.6 LLM regeneration of websites

| # | Source | What it is | Why it's relevant |
|---|--------|------------|-------------------|
| 17 | **Design2Code** — Si et al., Stanford, 2024. [arXiv:2403.03163](https://arxiv.org/abs/2403.03163) | Benchmark + methods for generating website HTML/CSS from screenshots with LLMs; GPT-4oV rated "could replace the original" 49% of the time; text-augmented prompting improves fidelity; found training on raw scraped pages unstable (long, noisy code). | Direct prior art for the front-end half of our regeneration pipeline. Two lessons: (a) current models regenerate plausible front-ends at rates good enough for a training environment; (b) our regenerated/cleaned code is actually an advantage over raw crawls for downstream use. |
| 18 | **WebCode2M** — 2024 | Large-scale dataset of webpage screenshots paired with code for training code-generation models. | Shows the screenshot→code data flywheel at scale exists; our crawl naturally produces (screenshot, DOM) pairs for it. |

---

## 3. Design Plan: The Novel Component

**The gap:** static captures (InSTA) can't support transactional tasks or verifiable rewards; functional replicas (WebArena) have both but were hand-built for 6 sites; API-level mock worlds (AppWorld/τ-bench) are stateful and verifiable but aren't the web (no browser surface, no search). Nobody has published a pipeline for *functionally-equivalent regeneration of real websites at scale, with seeded backends, behind a local search engine*. That is the novel part.

### 3.1 System architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ 0. CORPUS                                                            │
│    Common Crawl WET/WARC + rendered screenshots + link graph         │
│    (Optionally: own crawl of top sites for fresher/complete assets)  │
├─────────────────────────────────────────────────────────────────────┤
│ 1. EXTRACT                                                           │
│    Per site: main text (verbatim!), entities, link structure,        │
│    page inventory, media assets                                      │
├─────────────────────────────────────────────────────────────────────┤
│ 2. PLAN   (LLM agent)                                                │
│    Per site: sitemap/page graph, proposed data schema,               │
│    interaction inventory (forms, search, auth, cart, ...)            │
│    Output: structured site spec (JSON)                               │
├─────────────────────────────────────────────────────────────────────┤
│ 3. GENERATE   (LLM agent, heavily constrained)                       │
│    Front-end (HTML/CSS/JS from screenshots+DOM, Design2Code style)   │
│    + thin CRUD back-end (fixed framework: FastAPI + SQLite)          │
│    seeded with entities from step 1                                  │
│    Tier A: full functional rebuild (top N sites)                     │
│    Tier B: static-served captures with functional search box only    │
├─────────────────────────────────────────────────────────────────────┤
│ 4. VERIFY → REPAIR   (load-bearing loop)                             │
│    Playwright functional tests: links resolve, forms persist,        │
│    site search returns relevant results, auth flow works             │
│    Render-diff score vs. original (Design2Code metrics)              │
│    Failures fed back to repair agent; N repair rounds max            │
├─────────────────────────────────────────────────────────────────────┤
│ 5. HOST                                                              │
│    Tier A: per-site Docker containers (browser-realistic)            │
│    Rollout mode: in-process FastAPI TestClient (AppWorld pattern,    │
│    ~free) for high-throughput RL                                     │
│    Per-episode DB snapshot reset (SQLite copy / overlay)             │
├─────────────────────────────────────────────────────────────────────┤
│ 6. INDEX / SEARCH                                                    │
│    BM25 index (Meilisearch/Elasticsearch) + dense retriever          │
│    Served as a tool: search(query) -> ranked snippets+URLs           │
│    (Search-R1 retriever-server pattern; R1-Searcher evidence)        │
├─────────────────────────────────────────────────────────────────────┤
│ 7. TASKS                                                             │
│    LLM-proposed tasks per site (InSTA pattern)                       │
│    Where possible: programmatic goal-state checkers (τ-bench/        │
│    WebArena pattern) — final DB state vs gold state                  │
│    Fallback: LLM judge (InSTA)                                       │
├─────────────────────────────────────────────────────────────────────┤
│ 8. TRAIN                                                             │
│    SFT on filtered trajectories → RL (GRPO/PPO)                      │
│    Reward = state-diff (AppWorld) + milestones (ToolSandbox)         │
│              - minefields (ToolSandbox) - collateral damage          │
│    Tool sandbox hardening for any live calls (Tongyi: QPS, cache,    │
│    retry, failover)                                                  │
└─────────────────────────────────────────────────────────────────────┘
```

### 3.2 Key design decisions

1. **Behavioral equivalence, not visual.** Agents act on DOM structure and affordances, not pixels. Target: the tasks a human can do on the original site remain doable; the DOM remains structurally similar. Visual similarity is a soft signal (render-diff) in the repair loop, not an objective.
2. **Content verbatim.** Crawl-extracted text goes into the seeded DB unparaphrased. This guards against the sneaky sim-to-real gap where agents overfit to the generator's aesthetic and linguistic priors.
3. **Constrained generation.** Fixed back-end framework, fixed patterns, small allowed dependency set. This is what makes automated verification feasible at scale — the test harness knows what "correct" looks like.
4. **Tiered fidelity.** Full functional rebuilds for a top-N set (start 1–5k sites); static long tail indexed by search; everything searchable either way. 150k functional replicas is a budget-killer; 150k indexed pages + 5k functional hubs is achievable.
5. **Verification loop is the product.** Without generate → test → repair, error rates compound across thousands of sites and the corpus quietly rots. Budget most engineering here.
6. **Serverless-first execution.** In-process FastAPI (AppWorld pattern) for RL rollouts; Docker-served only when browser realism is required. Snapshot-per-episode DBs give resettability.

### 3.3 Reward design (borrowing wholesale)

- **State-diff reward** (τ-bench/Klavis/AppWorld): compare final DB state to gold state.
- **Milestones** (ToolSandbox): DAG of intermediate required states from per-turn snapshots → dense reward for long horizons.
- **Minefields** (ToolSandbox): specified forbidden events → hard zero. Counters reward hacking.
- **Collateral-damage check** (AppWorld): penalize state changes outside the task's scope.
- **Reliability metric** (τ-bench): pass^k over k rollouts, not just mean success.

### 3.4 Validation strategy

1. Train in the mock env; evaluate on WebArena (functional replica transfer) and OSWorld (out-of-distribution).
2. Evaluate search behavior against a live engine (R1-Searcher protocol) — mock→live search transfer has positive evidence; measure the gap explicitly.
3. Small live-site pilot for end-to-end sim-to-real.

### 3.5 Risks

| Risk | Mitigation |
|------|------------|
| Compounding generation errors at scale | Verification/repair loop is load-bearing; quality gates per site before admission to corpus |
| Generator-prior overfitting | Verbatim content; diversity audits; live-site validation |
| Search sim-to-real gap (result quality differs from Google) | Dense+BM25 ensemble; live-engine validation; treat as known gap in paper |
| Cost of full functional tier | Tiering (3.2.4); per-site cost model; reject sites whose repair loop doesn't converge |
| Legal/licensing of crawled content | Prefer Common Crawl-derived content; internal research use; per-site robots/terms review for the functional tier |
| Reward hacking | Minefields + collateral-damage + pass^k |

### 3.6 De-risking order (suggested build sequence)

1. **Search tier first** (Search-R1 pattern over Common Crawl/Wikipedia) — cheapest, and R1-Searcher shows it transfers.
2. **Static tier** (InSTA-style serves of captures) + search — validates that search+static training already yields transfer (InSTA evidence suggests it will).
3. **Functional tier** for top-N sites with the full verify/repair loop — the novel contribution.
4. **Reward stack** (state-diff + milestones + minefields) and RL on top.

Steps 1–2 are mostly integration; step 3 is the paper.

---

## 4. Open questions

- Per-site regeneration cost at quality-bar convergence — needs a pilot on 50 sites.
- Do LLM-regenerated DOMs preserve the affordance structure agents need, or should generation be conditioned on the original DOM rather than screenshots?
- Interaction coverage: which site interactions (auth, payments, realtime feeds) are worth supporting in v1 vs. mocking as dead ends?
- Should the search index include the long-tail static tier only, or also Tier-A dynamic content (reindex per episode state)?
