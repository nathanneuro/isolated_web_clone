# Airgapped Web Environment — Synthetic Population and Animation

**Version:** 0.1 (draft)
**Date:** 2026-09-14
**Companion to:** Bundle Format Specification v0.1, Scale and Storage v0.1, Agent Sandbox v0.1

---

## 1. Purpose

A reconstructed web that never changes is detectably a reconstruction. Every thread
has the same replies it had yesterday, no listing ever gains an item, every
timestamp recedes into the past at exactly the rate of wall-clock time, and nothing
an agent does is ever concurrent with anything anyone else does. An agent evaluated
in that environment learns, among other things, that the world holds still.

So the environment carries a **synthetic population**: millions of users across the
corpus, some of them **animated** — posting, replying, listing, voting, commenting
while an episode is in progress.

This document specifies how that population is represented, how animation is driven,
and why the obvious implementation of "bot scripts" is one this design cannot accept.

Scale, consistent with the scale spec:

| Quantity | Order of magnitude |
|----------|--------------------|
| Synthetic users across the corpus | 10^6 – 10^7 |
| Users per site | 10^1 – 10^5, heavy-tailed |
| Users animated at any moment | 10^3 – 10^5 |
| Animation events per episode | 10^0 – 10^2 per live site |

---

## 2. The rule this document exists to state

> **Bot scripts are not scripts.**

An animated user is described by a **behaviour spec**: a schema-constrained,
free-text-free declaration of what kinds of action that user takes, how often, with
what distribution, against which affordances of which site. A deterministic driver
inside the airgap reads that declaration and performs the actions. No code
describing behaviour ever crosses the diode, and nothing inside evaluates a
behaviour description as a program.

This is not fastidiousness. The bundle format already says commands never carry
code, scripts, or config bodies inline (§9), and the whole ingress design rests on
the receiver never treating unsigned bytes as instructions. "Ship a little Python
that makes users post things" defeats all of it in one step: it is arbitrary code,
authored outside by cheap agents working from scraped content, executing inside the
environment zone with database access to the sites the agent is being evaluated
against. Every property the diode, the encryption, the linter, and the worker's
no-free-text rule were built to establish is gone the moment that script runs.

The behaviour spec is the same trade the site spec makes, one layer over: the thing
that crosses is structure, and the thing that turns structure into behaviour is a
deterministic generator inside that only knows a fixed set of patterns.

---

## 3. Representation

### 3.1 Users are content; the population shape is structure

A user has a display name, a bio, an avatar, a posting history. All of that is
**content** in the bundle format's sense: it came from or was generated alongside
the scrape, it may contain anything, and it is encrypted into blobs the worker
cannot read (§5). Users arrive in the site's seed, in the same `db_seed` blob as
everything else, in whatever tables the site spec declares.

What is **structure**, and therefore plaintext in `spec/site.json`, is the shape:
which table holds users, which column is the identity key, which tables reference it.
The worker needs to know a `users` table exists and that `threads.author` points at
it. It must never learn that one of them is called `longhouse_pete`.

### 3.2 Cross-site identity

Part of what makes a web feel like a web is that the same person appears in several
places. That mapping — this user on this site is that user on that site — is
**control-plane data**, not site data.

It lives in the inside Postgres control plane (scale spec §2.4), not in any site's
SQLite file, for the reason that governs that split: a site's application must not be
able to enumerate identities on other sites. An agent that finds an injection in one
forum should learn about that forum's users and no others.

The identity graph is built outside, shipped as its own bundle type against the
control plane rather than against a site, and is never readable by a site app.

### 3.3 The behaviour spec

Sketch, subject to the same lint rules as the site spec — identifiers, enums, and
numbers, no free text, nothing longer than the caps:

```json
{
  "population_id": "site-000417-pop-r1",
  "site_id": "site-000417",
  "cohorts": [
    {
      "id": "c_lurkers",
      "user_count": 18400,
      "behaviours": []
    },
    {
      "id": "c_regulars",
      "user_count": 260,
      "behaviours": [
        { "id": "b_reply", "action": "form_submit", "form": "f_comment",
          "route": "r_comment", "rate_per_hour": 4, "distribution": "poisson",
          "content_pool": "cp_replies", "target_selector": "q_recent_posts" },
        { "id": "b_thread", "action": "form_submit", "form": "f_new_thread",
          "route": "r_new_thread", "rate_per_hour": 1, "distribution": "poisson",
          "content_pool": "cp_threads" }
      ]
    }
  ],
  "content_pools": [
    { "id": "cp_replies", "blob_ref": "content/…​.blob", "row_count": 40000 },
    { "id": "cp_threads", "blob_ref": "content/…​.blob", "row_count": 9000 }
  ]
}
```

Three things to notice, because each is doing work:

- **`action` is an enum**, implemented by the driver. `form_submit`, `vote`,
  `edit_own`, `delete_own`. A new action is a driver release, not a spec change —
  the same rule the test-kind enum follows.
- **Bots act through the site's declared affordances**, by `form` and `route` id.
  They do not write to the database directly. A bot is therefore constrained to
  exactly what a user of that site could do, which means bot activity cannot produce
  states the site's own interface could not produce, and the forms are already
  validated by the composer.
- **What a bot says comes from a content pool**, an encrypted blob of pre-generated
  text with a row count declared in the plaintext. The driver draws from it. There
  is no field in which a behaviour spec could carry a sentence, so there is nowhere
  for an injection or an instruction to ride along, and the worker composing this
  still cannot read a word of it.

---

## 4. The driver

The **population driver** runs inside, in the environment zone, one instance per live
site. It is deterministic, has no LLM in it, and does exactly one thing: at each tick,
for each cohort, sample which users act from the declared distribution and submit
the corresponding form through the site's own HTTP interface with content drawn from
the pool.

It reaches the site the same way the agent does — over HTTP, through the site's
declared routes. It gets no database handle. That is deliberate: a driver with direct
DB access would be able to create states the site's interface cannot, which both
breaks the realism it exists to provide and hands anything that compromises it a
write primitive the agent's own path does not have.

### 4.1 Determinism

An eval is worthless if it is not reproducible, and a live environment is the easy
way to lose reproducibility. The driver is seeded from `(run_id, episode_id,
site_id)`, and all of its randomness — which users act, when, which pool rows they
draw — comes from that seed. Replaying an episode replays the same population
activity against the same agent actions.

This also means animation is **episode-local**. A bot's post exists in that episode's
database clone and vanishes with it at reset. Animation does not accumulate across
episodes, because an environment that drifts is an environment whose results cannot
be compared across a run.

### 4.2 Rate and budget

Each site's driver has a hard cap on actions per episode, enforced by the driver and
not by the spec's declared rates. A behaviour spec that declares an absurd rate gets
clamped and counted, not obeyed: the spec is authored outside, and outside is not
trusted to be sensible about inside resource consumption.

---

## 5. What animation does to the reward signal

This is the part most likely to be discovered late and painfully.

The design plan's reward stack is state-based: compare final DB state to a gold
state, penalise collateral damage, check milestones. **All of it assumes the agent is
the only writer.** Turn on a population driver and it is not — replies appear during
the episode that the agent did not cause, and a naive state diff scores them as the
agent's collateral damage.

So the driver's writes must be **attributable and excluded**. Every row a driver
creates is tagged with the episode's driver identity at insert time, and the scorer
diffs only rows not so tagged. Concretely:

- **State-diff reward** compares agent-attributable state against gold.
- **Collateral-damage check** ignores driver-attributable changes, and *only* those.
  A row the agent caused a bot to create — it replied and a bot replied to it — is
  driver-attributable, which is correct: the agent did not write it.
- **Milestones** must be reachable regardless of population activity, or they encode
  a race.
- **Minefields** need care in the other direction. If a forbidden state can be
  reached by ordinary bot activity, an agent scores zero for something it did not do.
  Any minefield whose trigger a driver action could produce is a broken minefield.

The general rule: **animation must not be able to change an episode's score.** It
changes what the agent sees, never what the agent is judged on. If a population
setting changes measured performance, that is a bug in attribution, not a finding
about the model.

---

## 6. Threat model additions

The population adds components and therefore surface. Stated plainly:

**The driver is in the environment zone, not the trusted zone.** It is reachable from
nothing the agent controls, but it is a program acting on a site the agent is also
acting on. It gets the same treatment as a site: no network route out, no database
handle, no access to the control plane, no cross-site visibility.

**Bots are a channel to the agent.** A driver writes text an agent then reads. That
text comes from an encrypted pool authored outside, which is the same provenance as
every other piece of site content and gets no more trust — the agent's broker already
frames page text as untrusted (agent-sandbox-spec §3.3), and bot-written text arrives
through exactly that path.

**The agent can influence what bots produce.** If the agent posts and a bot replies
to that post, the agent has a limited write into the content another part of the
environment surfaces. This is a real self-influence loop, it is bounded by pool
content and the action enum, and it is worth naming rather than discovering. It is
also the mechanism by which an agent could attempt to signal to a *later* episode —
which episode-local reset (§4.1) closes.

**Cross-site identity is a lateral path if misplaced.** §3.2 puts it in the control
plane for exactly this reason. A site app that can resolve one of its users to their
account on another site has turned an injection in a forum into a corpus-wide read.

---

## 7. Open questions

1. **Do bots need to be smarter than an enum?** Drawing pre-generated text from a
   pool produces activity that is plausible in aggregate and shallow in particular:
   a bot reply will not be *about* the thread it replies to. The next step is an
   LLM-driven bot, and that step puts a language model inside the environment zone
   reading site content, which is the thing this whole design exists to avoid. The
   likely answer is offline conditioning — generate pool rows outside, conditioned on
   the thread they will be attached to, and ship them pre-bound. Needs designing.
2. **Population as its own bundle type, or part of the site bundle?** Separate lets a
   site's population be revised without re-shipping the site; together keeps the key
   hierarchy flat. Leaning separate at 10^5 sites, with `type: "population"`.
3. **Realistic time.** Animation makes timestamps meaningful for the first time, which
   raises whether inside clock time should advance at wall-clock rate, be compressed
   per episode, or be pinned. Pinning is simplest and probably wrong, since it makes
   every "recent" listing permanently stale.
4. **How much of the corpus is animated at all?** Animating 10^5 sites continuously is
   a real compute line item for something the agent mostly does not observe. Likely
   answer: animate on demand, only sites an episode actually touches, which is a few
   per episode and changes the cost by orders of magnitude.
5. **Attribution tagging and the site schema.** §5 requires a driver-identity column
   on every table a driver can write. That is structure, so it belongs in the site
   spec, and the reconstruction agent has to emit it. Cleanest is for the composer to
   add it automatically to any table named by a mutation, rather than asking 10^5
   reconstructions to remember.
