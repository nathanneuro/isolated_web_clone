"""Ambient population and eval choreography, executed deterministically.

Two layers with different authors (spec §2). Ambient behaviour is sampled from
declared rates; choreography is a schedule keyed to the episode step, because a
question about racing a deadline must be reproducible rather than sampled.

Three properties the code exists to hold:

**Determinism.** All randomness comes from a seed derived from
`(run_id, episode_id, site_id)`. Replaying an episode replays the same population
activity against the same agent actions. An eval that does not reproduce is not a
measurement.

**Attribution.** Every write carries the driver's tag in a header, so the scorer can
exclude it (spec §6). Animation changes what the agent sees, never what it is judged
on.

**Budget.** The action cap is enforced here, not by the declared rates. A behaviour
spec is authored outside, and outside is not trusted to be sensible about inside
resource consumption.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field

DRIVER_TAG_HEADER = "X-Driver-Tag"
MAX_ACTIONS_PER_EPISODE = 50

# The action enum the driver implements. A behaviour naming anything else is a
# driver-release ticket, not a spec change (spec §3.2).
SUPPORTED_ACTIONS = frozenset({"form_submit"})

TARGET_ID = re.compile(r'data-thread-id="(\d+)"')


@dataclass(frozen=True)
class Behaviour:
    id: str
    action: str
    form: str
    route: str
    content_pool: str
    rate_per_hour: float = 0.0
    distribution: str = "poisson"

    def __post_init__(self) -> None:
        assert self.action in SUPPORTED_ACTIONS, f"unsupported action: {self.action}"
        assert self.distribution in ("poisson", "uniform"), self.distribution
        assert 0 <= self.rate_per_hour <= 10_000, self.rate_per_hour


@dataclass(frozen=True)
class Cohort:
    id: str
    user_count: int
    behaviours: tuple[Behaviour, ...] = ()


@dataclass(frozen=True)
class Population:
    """Ambient liveness. Pipeline-signed, ships with the site."""

    population_id: str
    site_id: str
    cohorts: tuple[Cohort, ...] = ()


@dataclass(frozen=True)
class ScriptStep:
    at_step: int
    action: str
    form: str
    route: str
    content_pool: str
    pool_row: int = 0

    def __post_init__(self) -> None:
        assert self.action in SUPPORTED_ACTIONS, f"unsupported action: {self.action}"
        assert self.at_step >= 0, self.at_step


@dataclass(frozen=True)
class Actor:
    id: str
    user_ref: str
    script: tuple[ScriptStep, ...] = ()


@dataclass(frozen=True)
class Choreography:
    """Per-question schedule. Dev-signed, ships with the eval definition."""

    choreography_id: str
    site_id: str
    question_id: str
    actors: tuple[Actor, ...] = ()
    ambient: str = "suppress_for_actors"

    def __post_init__(self) -> None:
        assert self.ambient in ("suppress_for_actors", "suppress_all", "allow"), self.ambient


@dataclass
class DriverCounters:
    actions_performed: int = 0
    actions_clamped: int = 0
    ambient_suppressed: int = 0
    targets_unavailable: int = 0
    post_failures: int = 0

    def as_metrics(self) -> dict[str, int]:
        return {f"driver.{k}": v for k, v in vars(self).items()}


@dataclass
class PerformedAction:
    step: int
    actor: str
    behaviour: str
    route: str
    status: int


class PopulationDriver:
    """Drives one site for one episode.

    Holds an HTTP client and nothing else. There is deliberately no database
    handle: writes go through the site's declared forms, so the driver is
    constrained to exactly what a user of that site could do.
    """

    def __init__(
        self,
        client,
        spec: dict,
        *,
        run_id: str,
        episode_id: str,
        site_id: str,
        population: Population | None = None,
        choreography: Choreography | None = None,
        pools: dict[str, list[dict]] | None = None,
        max_actions: int = MAX_ACTIONS_PER_EPISODE,
        tick_seconds: int = 60,
    ) -> None:
        self._client = client
        self._spec = spec
        self.population = population
        self.choreography = choreography
        self.pools = pools or {}
        self.max_actions = max_actions
        self.tick_seconds = tick_seconds
        self.counters = DriverCounters()
        self.performed: list[PerformedAction] = []

        # Seed from the episode identity, never from wall-clock or object ids.
        digest = hashlib.blake2b(
            f"{run_id}|{episode_id}|{site_id}".encode(), digest_size=8
        ).digest()
        self._rng = random.Random(int.from_bytes(digest, "big"))
        self._routes = {r["id"]: r for r in spec["routes"]}
        self._forms = {f["id"]: f for f in spec.get("forms", [])}

    # -- public ------------------------------------------------------------

    def tick(self, step: int) -> list[PerformedAction]:
        """Run one episode step's worth of population activity."""
        performed: list[PerformedAction] = []
        performed += self._run_choreography(step)
        performed += self._run_ambient(step)
        self.performed += performed
        return performed

    @property
    def suppressed_actors(self) -> frozenset[str]:
        if self.choreography is None:
            return frozenset()
        if self.choreography.ambient == "suppress_for_actors":
            return frozenset(a.user_ref for a in self.choreography.actors)
        return frozenset()

    # -- layers ------------------------------------------------------------

    def _run_choreography(self, step: int) -> list[PerformedAction]:
        if self.choreography is None:
            return []
        performed = []
        for actor in self.choreography.actors:
            for script_step in actor.script:
                if script_step.at_step != step:
                    continue
                content = self._pool_row(script_step.content_pool, script_step.pool_row)
                if content is None:
                    continue
                action = self._submit(
                    step, actor.id, script_step.form, script_step.route, content
                )
                if action:
                    performed.append(action)
        return performed

    def _run_ambient(self, step: int) -> list[PerformedAction]:
        if self.population is None:
            return []
        if self.choreography and self.choreography.ambient == "suppress_all":
            self.counters.ambient_suppressed += 1
            return []

        performed = []
        for cohort in self.population.cohorts:
            for behaviour in cohort.behaviours:
                expected = behaviour.rate_per_hour * cohort.user_count * self.tick_seconds / 3600
                count = self._sample(expected, behaviour.distribution)
                for _ in range(count):
                    content = self._pool_row(
                        behaviour.content_pool, self._rng.randrange(1 << 30)
                    )
                    if content is None:
                        continue
                    action = self._submit(
                        step, cohort.id, behaviour.form, behaviour.route, content
                    )
                    if action:
                        performed.append(action)
        return performed

    def _sample(self, expected: float, distribution: str) -> int:
        if expected <= 0:
            return 0
        if distribution == "uniform":
            return int(expected)
        # Knuth's Poisson, driven by the seeded RNG so the draw is reproducible.
        limit, count, product = pow(2.718281828459045, -expected), 0, self._rng.random()
        while product > limit and count < 1000:
            count += 1
            product *= self._rng.random()
        return count

    # -- acting ------------------------------------------------------------

    def _submit(
        self, step: int, actor: str, form_id: str, route_id: str, content: dict
    ) -> PerformedAction | None:
        if self.counters.actions_performed >= self.max_actions:
            # The cap is the driver's, not the spec's. A spec that declares an
            # absurd rate gets clamped and counted, never obeyed.
            self.counters.actions_clamped += 1
            return None

        route = self._routes.get(route_id)
        form = self._forms.get(form_id)
        if route is None or form is None:
            self.counters.post_failures += 1
            return None

        path = self._resolve(route["path"])
        if path is None:
            self.counters.targets_unavailable += 1
            return None

        data = {
            field_spec["name"]: str(content.get(field_spec["name"], ""))
            for field_spec in form["fields"]
        }
        response = self._client.post(
            path,
            data=data,
            headers={DRIVER_TAG_HEADER: actor},
            follow_redirects=False,
        )
        if response.status_code >= 400:
            self.counters.post_failures += 1
            return None

        self.counters.actions_performed += 1
        return PerformedAction(step, actor, form_id, path, response.status_code)

    def _resolve(self, path: str) -> str | None:
        """Fill path params by reading the site, never by reading the database."""
        if "{" not in path:
            return path
        listing = self._client.get("/", follow_redirects=False)
        targets = TARGET_ID.findall(listing.text)
        if not targets:
            return None
        chosen = self._rng.choice(sorted(set(targets), key=int))
        return re.sub(r"\{[a-z_]+\}", chosen, path)

    def _pool_row(self, pool_id: str, index: int) -> dict | None:
        rows = self.pools.get(pool_id)
        if not rows:
            return None
        return rows[index % len(rows)]
