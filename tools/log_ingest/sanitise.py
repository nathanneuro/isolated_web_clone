"""Produce a display-safe copy of an untrusted payload.

The original is kept byte-exact, because it is evidence. This module produces the
*derived* copy a human actually reads (log-diode-spec §5.2).

The threat is not theoretical and is not about markup. A terminal is an interpreter:
a payload containing ANSI and OSC sequences can retitle the window, move the cursor
up and overwrite lines that already scrolled past, set colours that render text
invisible, or on some terminals trigger a reply the shell then reads as input. An
agent whose output lands in a log a human tails has a channel to that human's screen,
and "hide the line recording what I just did" is a natural thing for an optimiser to
find.

So: strip the control bytes rather than escaping them. Escaping preserves the
sequence for whatever renders it next, and there is always something next.
"""

from __future__ import annotations

# C0 minus the three whitespace bytes worth keeping, plus DEL and the C1 range.
_C0_KEEP = {0x09, 0x0A, 0x0D}  # tab, newline, carriage return
_STRIP = (
    {b for b in range(0x00, 0x20) if b not in _C0_KEEP}
    | {0x7F}
    | set(range(0x80, 0xA0))
)

# U+2028/2029 are line separators some renderers honour, and the bidirectional
# overrides are the "Trojan Source" family: they reorder displayed text without
# changing bytes, so what a reviewer reads is not what is there.
_STRIP_CHARS = {
    " ", " ",
    "‪", "‫", "‬", "‭", "‮",
    "⁦", "⁧", "⁨", "⁩",
    "‎", "‏",
    "﻿",
}

DEFAULT_MAX_CHARS = 8192
REPLACEMENT = "�"


def sanitise_for_display(
    payload: bytes, max_chars: int = DEFAULT_MAX_CHARS
) -> tuple[str, int]:
    """Return `(safe_text, strip_count)`.

    `strip_count` feeds `log.sanitiser_strips`. A payload that needed a lot of
    stripping is itself interesting: ordinary log text needs none.
    """
    # errors="replace" rather than a decode that can raise. Invalid UTF-8 is
    # expected here, since the payload may be binary or deliberately malformed.
    text = payload.decode("utf-8", errors="replace")

    stripped = 0
    out: list[str] = []
    for char in text:
        code = ord(char)
        if code in _STRIP or char in _STRIP_CHARS:
            stripped += 1
            continue
        out.append(char)

    safe = "".join(out)
    if len(safe) > max_chars:
        safe = safe[:max_chars] + f"\n[truncated {len(safe) - max_chars} chars]"
    return safe, stripped


def is_display_safe(text: str) -> bool:
    """Assertion helper: does this string still contain anything a terminal acts on?"""
    return not any(
        ord(c) in _STRIP or c in _STRIP_CHARS for c in text
    )
