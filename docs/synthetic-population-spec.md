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

Animation comes from two places with different authors and different trust, and §2
separates them: ambient liveness shipped by the reconstruction pipeline with the
site, and per-question **eval choreography** shipped by a researcher with the eval
definition, making the users a question depends on behave in an exactly specified
way.

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

## 2. Two layers of animation, from two different places

These are easy to conflate and must not be, because they have different authors,
different trust, and different lifetimes.

### 2.1 Ambient population — from the mock-maker

Background liveness. Threads gain replies, listings gain items, the site does not
look embalmed. It is a property of the *site*, produced by the reconstruction
pipeline alongside the seed, shipped in the site bundle, signed with the **pipeline
key**. It is not about any particular eval, it is the same for every episode that
touches that site, and it exists so the corpus does not read as frozen.

### 2.2 Eval choreography — from the eval definition

This is the interesting one. An eval question is rarely "browse this site"; it is
something like *the agent must find where this user announced this thing and reply
before this other user does*. Setting that up means making **the users relevant to
this eval behave in a specific, deterministic way, per eval question**.

That is not the mock-maker's business and cannot be. The reconstruction pipeline
built this site months ago from a crawl, has no idea what will be evaluated on it,
and is a swarm of cheap agents nobody is reading the output of. Eval choreography is
authored by **a researcher designing a question**, ships as part of the eval
definition, and is signed with a **dev key** — the `run_config` path of
bundle-format-spec §9, referenced by a signed `start_run` command.

So the provenance is:

| | Ambient population | Eval choreography |
|---|---|---|
| Author | reconstruction pipeline (cheap agents) | a researcher |
| Signed with | pipeline key | dev key |
| Ships in | the site bundle | the eval definition (`run_config`) |
| Scope | the whole site, every episode | the users relevant to one question |
| Lifetime | the site revision | the eval |
| Determinism | seeded, reproducible | **exactly specified, per question** |

Both are executed by the same driver (§4) inside the per-site sandbox. Choreography
overrides ambient behaviour for the users it names, and ambient behaviour continues
for everyone else.

---

## 3. Why choreography is declared, not scripted

The functional requirement is determinism: an eval question that does not reproduce
is not a measurement. A declared schedule plus a seeded driver gives byte-identical
population activity for a given `(run_id, episode_id, site_id)`, which is exactly
what "behave in this deterministic way" asks for. Scripts can be deterministic too,
but only if every source of nondeterminism in them has been found, and the cost of
being wrong is a result nobody can reproduce.

The security requirement is sharper, and it is about the channel rather than the
content.

**The dev command channel is the highest-authority path into the airgap.** Everything
else entering is either content the worker cannot read or structure a linter
constrains. A dev-signed command is the one thing inside acts on as an instruction.
Bundle-format-spec §9 already states the consequence: *commands never carry code,
scripts, or config bodies inline*, and `config_ref` points at something that already
exists inside.

That rule exists for exactly this case. If a dev key can ship arbitrary code that
executes inside, then **a compromised researcher workstation is arbitrary code
execution inside the airgap** — not in an environment-zone sandbox next to a
generated forum, but arriving through the channel the system is built to trust. That
is a far worse outcome than a compromised site app, and it is reachable by phishing
one person.

So choreography is a declaration the driver interprets, and the driver is a fixed,
reviewed program inside. A researcher describes *what the users do*; they do not ship
a program that does it.

### 3.1 The lines that actually matter

Containment is a property of where animation runs, not of whether it was declared.
These apply to any animation mechanism:

- It runs **inside the per-site sandbox**, never beside it. A driver process with
  reach across sites turns one site's bug into a corpus-wide write.
- It gets **no database handle**, only the site's own HTTP interface (§5). Direct DB
  access lets it create states the site's interface cannot, which breaks the realism
  it exists to provide and hands anything that compromises it a write primitive the
  evaluated agent does not have.
- It never executes in the **control plane or any trusted zone**.
- The **worker never reads it**, for the same reason the worker reads no content.

### 3.2 When a question needs something the vocabulary cannot express

This will happen, and the answer is the same one `site-reconstruct` gives for schema
gaps: it is a ticket, not a workaround. A researcher who needs a behaviour the action
enum cannot express files for a driver release. The pressure to add an `eval_hook`
field that takes a snippet will be real and continuous, and granting it reopens
exactly the channel §3 closes.

If the vocabulary is too poor to express the questions people want to ask, that is a
genuine finding and the enum should grow. It should grow by review, on a release
cadence, and not per question.

## 4. Representation

### 4.1 Users are content; the population shape is structure

A user has a display name, a bio, an avatar, a posting history. All of that is
**content** in the bundle format's sense: it came from or was generated alongside
the scrape, it may contain anything, and it is encrypted into blobs the worker
cannot read (§5). Users arrive in the site's seed, in the same `db_seed` blob as
everything else, in whatever tables the site spec declares.

What is **structure**, and therefore plaintext in `spec/site.json`, is the shape:
which table holds users, which column is the identity key, which tables reference it.
The worker needs to know a `users` table exists and that `threads.author` points at
it. It must never learn that one of them is called `longhouse_pete`.

### 4.2 Cross-site identity

Part of what makes a web feel like a web is that the same person appears in several
places. That mapping — this user on this site is that user on that site — is
**control-plane data**, not site data.

It lives in the inside Postgres control plane (scale spec §2.4), not in any site's
SQLite file, for the reason that governs that split: a site's application must not be
able to enumerate identities on other sites. An agent that finds an injection in one
forum should learn about that forum's users and no others.

The identity graph is built outside, shipped as its own bundle type against the
control plane rather than against a site, and is never readable by a site app.

### 4.3 Ambient population spec (pipeline-signed, ships with the site)

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

### 4.4 Eval choreography (dev-signed, ships with the eval definition)

The researcher's layer. It names specific users, specific affordances, and an
explicit schedule, because a question that depends on *when* something happens needs
the when to be stated rather than sampled.

```json
{
  "choreography_id": "eval-webarena-transfer-07-site-000417",
  "site_id": "site-000417",
  "question_id": "q_reply_before_rival",
  "actors": [
    { "id": "a_announcer", "user_ref": "u_00043117",
      "script": [
        { "at_step": 0,  "action": "form_submit", "form": "f_new_thread",
          "route": "r_new_thread", "content_pool": "cp_q7_announce", "pool_row": 0 }
      ]},
    { "id": "a_rival", "user_ref": "u_00081902",
      "script": [
        { "at_step": 12, "action": "form_submit", "form": "f_comment",
          "route": "r_comment", "target": "t_announcement",
          "content_pool": "cp_q7_rival", "pool_row": 0 }
      ]}
  ],
  "ambient": "suppress_for_actors",
  "content_pools": [
    { "id": "cp_q7_announce", "blob_ref": "content/…​.blob", "row_count": 1 },
    { "id": "cp_q7_rival",    "blob_ref": "content/…​.blob", "row_count": 1 }
  ]
}
```

Differences from the ambient layer, each load-bearing:

- **`at_step`, not `rate_per_hour`.** Choreography is a schedule keyed to the
  episode's step counter, not a sampled process. "The rival replies at step 12" is
  reproducible in a way "the rival replies about four times an hour" is not, and the
  question depends on the agent racing a fixed deadline.
- **`user_ref` names individuals**, resolved against the site's user table. A
  question about *this user* needs that user, not a draw from a cohort.
- **`ambient: suppress_for_actors`** stops background behaviour from firing on the
  users the question depends on. Without it, an ambient reply could satisfy or spoil
  the condition and the question would measure the driver's RNG.
- **`pool_row` is explicit.** Choreographed content is chosen by the researcher, not
  sampled — the announcement has to say the thing the question is about. It is still
  an encrypted pool row rather than an inline string, so the no-free-text rule holds
  and the same text can be reviewed outside where it was written.

The same action enum serves both layers. A choreography that needs an action the enum
lacks is §3.2's ticket.

---

## 5. The driver

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

### 5.1 Determinism

An eval is worthless if it is not reproducible, and a live environment is the easy
way to lose reproducibility. The driver is seeded from `(run_id, episode_id,
site_id)`, and all of its randomness — which users act, when, which pool rows they
draw — comes from that seed. Replaying an episode replays the same population
activity against the same agent actions.

This also means animation is **episode-local**. A bot's post exists in that episode's
database clone and vanishes with it at reset. Animation does not accumulate across
episodes, because an environment that drifts is an environment whose results cannot
be compared across a run.

### 5.2 Rate and budget

Each site's driver has a hard cap on actions per episode, enforced by the driver and
not by the spec's declared rates. A behaviour spec that declares an absurd rate gets
clamped and counted, not obeyed: the spec is authored outside, and outside is not
trusted to be sensible about inside resource consumption.

---

## 6. What animation does to the reward signal

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

## 7. Threat model additions

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
which episode-local reset (§5.1) closes.

**Cross-site identity is a lateral path if misplaced.** §3.2 puts it in the control
plane for exactly this reason. A site app that can resolve one of its users to their
account on another site has turned an injection in a forum into a corpus-wide read.

---

## 8. Open questions

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
