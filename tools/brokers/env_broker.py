"""Env broker: the agent zone's only path to the environment zone.

The counterpart to the action broker, and it enforces the mirror-image rule. The
action broker stops the agent authoring a prompt; this one stops the agent
authoring a request. It does not proxy arbitrary HTTP. It accepts an `Action` from
the fixed enum, performs the corresponding interaction against the live site, and
returns a structured `PageView`.

What this buys, concretely: the agent cannot reach a hostname that is not a
registered live site, cannot choose a method, cannot set a header, cannot follow a
redirect off-site, and cannot see a response body except as the bounded, structured
fields an Observation is built from. A compromised agent zone gets the affordances
of a browser pointed at one site, not a socket.

The one header on the wire is the broker's, not the agent's: every write it performs
is attributed as `agent`, which is the only value the scorer credits. Selectors are
resolved against the page the agent was shown, not a fresh fetch, so a live
population changing the page mid-step cannot redirect a click or a submit.

Everything it refuses is counted, because counters are what reach the dashboard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urlparse

from .action_broker import (
    MAX_PAGE_CHARS,
    Action,
    ActionKind,
    BrokerCounters,
    Element,
    Observation,
)
from .gate import BrokerGate

MAX_STEPS_PER_EPISODE = 100
# A path on the current site. `//` is a protocol-relative URL, not a path.
INTERNAL_PATH = re.compile(r"^/(?!/)[A-Za-z0-9_\-./?=&{}]{0,255}$")
# A link to another live site: scheme optional, hostname registered, path internal.
CROSS_SITE = re.compile(r"^(?:https?:)?//([a-z0-9][a-z0-9.\-]{0,61}\.internal)(/[A-Za-z0-9_\-./?=&{}]{0,255})?$")
WEB_SEARCH_PATH = "/websearch"
# Must match tools.compose_fastapi_sqlite_v1.WRITER_HEADER; asserted in the tests
# rather than imported, so the broker does not depend on the site framework.
AGENT_WRITER = {"x-writer": "agent"}
FIELD_TAGS = ("input", "textarea", "select")


class _PageParser(HTMLParser):
    """Extract visible text and interactive affordances. No rendering, no JS.

    Selectors preferred in the order site-reconstruct requires templates to provide
    them: `id`, then `data-*`. A positional selector would be unstable across the
    reconstruction's revisions and is not offered.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.elements: list[Element] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {k: (v or "") for k, v in attrs}
        if tag in ("script", "style"):
            self._skip += 1
            return

        selector = self._selector(attributes)
        if tag == "a":
            if selector:
                self.elements.append(Element(selector, "link"))
            href = attributes.get("href", "")
            if INTERNAL_PATH.match(href):
                self.links.append(href)
        elif tag == "input" and selector:
            self.elements.append(Element(selector, "input"))
        elif tag == "textarea" and selector:
            self.elements.append(Element(selector, "textarea"))
        elif tag == "button" and selector:
            self.elements.append(Element(selector, "button"))
        elif tag == "form" and selector:
            self.elements.append(Element(selector, "form"))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and (stripped := data.strip()):
            self.text.append(stripped)

    @staticmethod
    def _selector(attributes: dict[str, str]) -> str:
        return selector_for(attributes)


def selector_for(attributes: dict[str, str]) -> str:
    """The one rule for naming an element: `id`, else the first `data-*`, else nothing."""
    if ident := attributes.get("id"):
        return f"#{ident}"
    for name, value in attributes.items():
        if name.startswith("data-") and value:
            return f'[{name}="{value}"]'
    return ""


class _LinkParser(HTMLParser):
    """Find one anchor by selector and report its href."""

    def __init__(self, selector: str) -> None:
        super().__init__(convert_charrefs=True)
        self._wanted = selector
        self.href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {k: (v or "") for k, v in attrs}
        if tag == "a" and self.href is None and selector_for(attributes) == self._wanted:
            self.href = attributes.get("href", "")


class _FormParser(HTMLParser):
    """Find one form by selector and list its fields under the same selector rule
    the page parser used, so what the agent typed into is what gets posted."""

    def __init__(self, selector: str) -> None:
        super().__init__(convert_charrefs=True)
        self._wanted = selector
        self._inside = False
        self.action: str | None = None
        self.fields: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {k: (v or "") for k, v in attrs}
        if tag == "form":
            if self.action is None and selector_for(attributes) == self._wanted:
                self._inside = True
                self.action = attributes.get("action", "")
            return
        if self._inside and tag in FIELD_TAGS and (name := attributes.get("name")):
            self.fields.append((selector_for(attributes), name))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._inside = False


@dataclass
class PageView:
    """What the agent zone learns about a page. Bounded, structured, no raw body."""

    path: str
    status: int
    page_text: str
    elements: tuple[Element, ...] = ()
    links: tuple[str, ...] = ()


@dataclass
class EnvCounters:
    denied: int = 0
    off_site: int = 0
    bad_action: int = 0
    steps: int = 0
    writes: int = 0  # successful POSTs; not a registry metric, read by D10
    site_switches: int = 0  # moves between live sites; not a registry metric

    def as_metrics(self) -> dict[str, int]:
        return {"broker.env_denied": self.denied + self.off_site + self.bad_action}


class EnvBroker:
    """Drives live sites on the agent's behalf, one episode at a time.

    It starts on a home site. It may move to another site only through `resolve`,
    which answers for registered live hostnames and nothing else, and only by a
    link on a page it served or a fake-web search result. A hostname `resolve`
    does not know is off-site, counted, and refused. The agent never holds a
    socket; it holds a browser pointed at the registry.
    """

    def __init__(
        self,
        client,
        hostname: str,
        search_path: str = "/search",
        gate: BrokerGate | None = None,
        *,
        resolve: Callable[[str], object | None] | None = None,
        web_search=None,
    ) -> None:
        self._client = client
        self._clients = {hostname: client}
        self._resolve = resolve
        self._web_search = web_search
        self.hostname = hostname
        self.search_path = search_path
        self._gate = gate or BrokerGate()  # the watchdog's halt: refused and counted
        self.counters = EnvCounters()
        self._form_state: dict[str, str] = {}
        self.current_path = "/"
        self._last_body = ""
        self._synthetic: str | None = None  # a results page the broker itself rendered

    def observe(self) -> PageView:
        if self._gate.severed:
            self.counters.denied += 1
            return PageView(path=self.current_path, status=503, page_text="")
        if self._synthetic is not None:
            return self._parse(self.current_path, 200, self._synthetic)
        return self._get(self.current_path)

    # -- where a link may take the agent ----------------------------------------

    @staticmethod
    def _target(url: str) -> tuple[str | None, str] | None:
        """(hostname or None, path) for a URL the agent may follow; None otherwise."""
        match = CROSS_SITE.match(url)
        if match:
            return match.group(1), match.group(2) or "/"
        if INTERNAL_PATH.match(url):
            return None, url
        return None

    def _go(self, url: str) -> bool:
        """Move to a URL, switching site if it names one. False means refused."""
        target = self._target(url)
        if target is None:
            self.counters.off_site += 1
            return False
        host, path = target
        if host is not None and host != self.hostname:
            client = self._clients.get(host) or (self._resolve(host) if self._resolve else None)
            if client is None:
                self.counters.off_site += 1
                return False
            self._clients[host] = client
            self._client, self.hostname = client, host
            self._form_state.clear()
            self.counters.site_switches += 1
        self.current_path = path
        self._synthetic = None
        return True

    def apply(self, action: Action, elements: tuple[Element, ...]) -> PageView:
        """Perform one action. Anything not permitted is a no-op plus a counter."""
        self.counters.steps += 1
        if self._gate.severed:
            return self.observe()
        self._gate.check_canary(action.selector, action.text)  # D4
        selectors = {e.selector for e in elements}

        if action.kind is ActionKind.GOTO:
            self._go(action.selector)
            return self.observe()

        if action.kind is ActionKind.SEARCH:
            if self._web_search is not None:
                self._synthetic = self._results_page(action.text)
                self.current_path = f"{WEB_SEARCH_PATH}?q={action.text}"
            else:
                self.current_path = f"{self.search_path}?q={action.text}"
                self._synthetic = None
            return self.observe()

        if action.kind is ActionKind.CLICK:
            if action.selector not in selectors:
                self.counters.denied += 1
                return self.observe()
            target = self._href_for(action.selector)
            if target is None:
                self.counters.denied += 1
                return self.observe()
            self._go(target)
            return self.observe()

        if action.kind is ActionKind.TYPE:
            if action.selector not in selectors:
                self.counters.denied += 1
                return self.observe()
            self._form_state[action.selector] = action.text
            return self.observe()

        if action.kind is ActionKind.SUBMIT:
            return self._submit(action, selectors)

        if action.kind in (ActionKind.ANSWER, ActionKind.STOP, ActionKind.NOOP):
            return self.observe()

        self.counters.bad_action += 1
        self._gate.schema_violations += 1  # D9: a kind outside the enum reached us
        return self.observe()

    def _submit(self, action: Action, selectors: set[str]) -> PageView:
        if action.selector not in selectors:
            self.counters.denied += 1
            return self.observe()
        form_action, fields = self._form_for(action.selector)
        if form_action is None:
            self.counters.denied += 1
            return self.observe()
        # fields maps the selector the agent typed into -> the form field name it
        # posts as. Keying form state by field name instead would silently drop
        # every value, since the agent only ever sees selectors.
        data = {name: self._form_state.get(selector, "") for selector, name in fields}
        response = self._client.post(
            form_action, data=data, headers=AGENT_WRITER, follow_redirects=False
        )
        if response.status_code < 400:
            self.counters.writes += 1
            self._gate.agent_writes += 1  # D10 compares this with what the site attributes
        if response.status_code in (302, 303) and (location := response.headers.get("location")):
            if INTERNAL_PATH.match(location):
                self.current_path = location  # a site redirects within itself only
            else:
                self.counters.off_site += 1
        self._form_state.clear()
        self._synthetic = None
        return self.observe()

    def _get(self, path: str) -> PageView:
        if not INTERNAL_PATH.match(path):
            self.counters.off_site += 1
            path = "/"
            self.current_path = "/"
        response = self._client.get(path, follow_redirects=False)
        return self._parse(path, response.status_code, response.text)

    def _parse(self, path: str, status: int, body: str) -> PageView:
        self._last_body = body
        parser = _PageParser()
        parser.feed(body)
        return PageView(
            path=path,
            status=status,
            page_text=" ".join(parser.text)[:MAX_PAGE_CHARS],
            elements=tuple(parser.elements[:64]),
            links=tuple(parser.links),
        )

    def _results_page(self, query: str) -> str:
        """The fake-web results page, rendered by the broker. Structure is the
        broker's; titles are site content, which the agent sees anyway."""
        from html import escape

        items = "".join(
            f'<li class="result-item"><a id="result-{i}" href="//{hit.hostname}{hit.path}">{escape(hit.title)}</a>'
            f" <span>{hit.hostname}</span></li>"
            for i, hit in enumerate(self._web_search.search(query, limit=10), start=1)
        )
        return f'<html><body><h1>Web search</h1><ul id="web-results">{items}</ul></body></html>'

    def _href_for(self, selector: str) -> str | None:
        """Resolve a clickable selector to its href, on the page the agent saw."""
        parser = _LinkParser(selector)
        parser.feed(self._last_body)
        if parser.href is not None and self._target(parser.href) is not None:
            return parser.href
        return None

    def _form_for(self, selector: str) -> tuple[str | None, list[tuple[str, str]]]:
        """Return the form's action and its (selector, field_name) pairs, on the
        page the agent saw."""
        parser = _FormParser(selector)
        parser.feed(self._last_body)
        if parser.action is None or not INTERNAL_PATH.match(parser.action):
            return None, []
        return parser.action, parser.fields


def observation_from(
    view: PageView, task: str, step: int, counters: BrokerCounters | None = None,
    gate: BrokerGate | None = None,
) -> Observation:
    """Bridge a PageView into the action broker's Observation. Bounded by both."""
    from .action_broker import clamp_observation

    return clamp_observation(task, view.path, view.page_text, list(view.elements), step, counters, gate)
