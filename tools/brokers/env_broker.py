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

Everything it refuses is counted, because counters are what reach the dashboard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlparse

from .action_broker import (
    MAX_PAGE_CHARS,
    Action,
    ActionKind,
    BrokerCounters,
    Element,
    Observation,
)

MAX_STEPS_PER_EPISODE = 100
INTERNAL_PATH = re.compile(r"^/[A-Za-z0-9_\-./?=&{}]{0,255}$")


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
        if ident := attributes.get("id"):
            return f"#{ident}"
        for name, value in attributes.items():
            if name.startswith("data-") and value:
                return f'[{name}="{value}"]'
        return ""


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

    def as_metrics(self) -> dict[str, int]:
        return {"broker.env_denied": self.denied + self.off_site + self.bad_action}


class EnvBroker:
    """Drives one live site on the agent's behalf. One site, one episode."""

    def __init__(self, client, hostname: str, search_path: str = "/search") -> None:
        self._client = client
        self.hostname = hostname
        self.search_path = search_path
        self.counters = EnvCounters()
        self._form_state: dict[str, str] = {}
        self.current_path = "/"

    def observe(self) -> PageView:
        return self._get(self.current_path)

    def apply(self, action: Action, elements: tuple[Element, ...]) -> PageView:
        """Perform one action. Anything not permitted is a no-op plus a counter."""
        self.counters.steps += 1
        selectors = {e.selector for e in elements}

        if action.kind is ActionKind.GOTO:
            if not INTERNAL_PATH.match(action.selector):
                self.counters.off_site += 1
                return self.observe()
            self.current_path = action.selector
            return self.observe()

        if action.kind is ActionKind.SEARCH:
            self.current_path = f"{self.search_path}?q={action.text}"
            return self.observe()

        if action.kind is ActionKind.CLICK:
            if action.selector not in selectors:
                self.counters.denied += 1
                return self.observe()
            target = self._href_for(action.selector)
            if target is None:
                self.counters.denied += 1
                return self.observe()
            self.current_path = target
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
        return self.observe()

    def _submit(self, action: Action, selectors: set[str]) -> PageView:
        if action.selector not in selectors:
            self.counters.denied += 1
            return self.observe()
        body = self._get(self.current_path)
        form_action, fields = self._form_for(action.selector)
        if form_action is None:
            self.counters.denied += 1
            return body
        # fields maps the selector the agent typed into -> the form field name it
        # posts as. Keying form state by field name instead would silently drop
        # every value, since the agent only ever sees selectors.
        data = {name: self._form_state.get(selector, "") for selector, name in fields}
        response = self._client.post(form_action, data=data, follow_redirects=False)
        if response.status_code in (302, 303) and (location := response.headers.get("location")):
            if INTERNAL_PATH.match(location):
                self.current_path = location
            else:
                self.counters.off_site += 1
        self._form_state.clear()
        return self.observe()

    def _get(self, path: str) -> PageView:
        if not INTERNAL_PATH.match(path):
            self.counters.off_site += 1
            path = "/"
            self.current_path = "/"
        response = self._client.get(path, follow_redirects=False)
        parser = _PageParser()
        parser.feed(response.text)
        return PageView(
            path=path,
            status=response.status_code,
            page_text=" ".join(parser.text)[:MAX_PAGE_CHARS],
            elements=tuple(parser.elements[:64]),
            links=tuple(parser.links),
        )

    def _href_for(self, selector: str) -> str | None:
        """Resolve a clickable selector to its href, from the page, not the agent."""
        response = self._client.get(self.current_path, follow_redirects=False)
        ident = selector.lstrip("#")
        match = re.search(
            rf'<a[^>]*id="{re.escape(ident)}"[^>]*href="([^"]*)"', response.text
        ) or re.search(
            rf'<a[^>]*href="([^"]*)"[^>]*id="{re.escape(ident)}"', response.text
        )
        if match and INTERNAL_PATH.match(match.group(1)):
            return match.group(1)
        return None

    def _form_for(self, selector: str) -> tuple[str | None, list[tuple[str, str]]]:
        """Return the form's action and its (selector, field_name) pairs."""
        response = self._client.get(self.current_path, follow_redirects=False)
        ident = selector.lstrip("#")
        block = re.search(
            rf'<form[^>]*id="{re.escape(ident)}"[^>]*>(.*?)</form>', response.text, re.DOTALL
        )
        if not block:
            return None, []
        header = re.search(
            rf'<form[^>]*id="{re.escape(ident)}"[^>]*action="([^"]*)"', response.text
        )
        if not header or not INTERNAL_PATH.match(header.group(1)):
            return None, []

        fields: list[tuple[str, str]] = []
        for tag in re.finditer(r"<(?:input|textarea|select)\b[^>]*>", block.group(1)):
            name = re.search(r'name="([A-Za-z0-9_]+)"', tag.group(0))
            if not name:
                continue
            ident_attr = re.search(r'id="([A-Za-z0-9_\-]+)"', tag.group(0))
            field_selector = f"#{ident_attr.group(1)}" if ident_attr else f"#{name.group(1)}"
            fields.append((field_selector, name.group(1)))
        return header.group(1), fields


def observation_from(view: PageView, task: str, step: int, counters: BrokerCounters | None = None) -> Observation:
    """Bridge a PageView into the action broker's Observation. Bounded by both."""
    from .action_broker import clamp_observation

    return clamp_observation(task, view.path, view.page_text, list(view.elements), step, counters)
