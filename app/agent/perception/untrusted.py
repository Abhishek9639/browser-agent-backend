"""Fencing page content before it reaches a model (spec section 4.11).

Everything read from a page is hostile input. The prompt marks it as such by
wrapping it in ``<page_state>`` delimiters and telling the model, in the system
prompt, never to follow instructions found inside them.

That only works if the page cannot close the fence. A page containing the
literal text ``</page_state>`` would otherwise end the untrusted block early
and have everything after it read as trusted instruction, which is the prompt
equivalent of SQL injection. :func:`neutralize` is the escaping step that
makes the delimiter unforgeable.
"""

import re
import unicodedata
from typing import Final

__all__ = [
    "CLOSE_TAG",
    "OPEN_TAG",
    "neutralize",
    "strip_invisible",
    "wrap_page_state",
]

OPEN_TAG: Final = "<page_state>"
CLOSE_TAG: Final = "</page_state>"

# Any attempt at the delimiter, open or close, with or without attributes and
# whatever the casing. Matched loosely on purpose: the page has no legitimate
# reason to contain this string at all, so over-matching costs nothing.
_DELIMITER: Final = re.compile(r"<\s*/?\s*page_state", re.IGNORECASE)

# Zero-width and bidirectional formatting characters. A model reads the
# codepoints, not the pixels, so text that looks innocuous on screen can carry
# instructions a human reviewer would never see.
_INVISIBLE: Final = re.compile(
    "["
    "​-‏"  # zero-width space through right-to-left mark
    "‪-‮"  # bidi embedding and override
    "⁠-⁤"  # word joiner, invisible operators
    "⁪-⁯"  # deprecated formatting
    "﻿"  # byte order mark
    "￹-￻"  # interlinear annotation
    "]"
)


def strip_invisible(text: str) -> str:
    """Remove characters that carry meaning without being visible.

    Two families. Zero-width and bidi controls, which can hide a sentence
    inside what looks like a product name. And C0/C1 control characters, which
    can break the prompt's own line structure. Tab, newline and carriage
    return survive, because losing those would mangle legitimate page text.
    """
    cleaned = _INVISIBLE.sub("", text)
    return "".join(
        char for char in cleaned if char in "\t\n\r" or unicodedata.category(char) != "Cc"
    )


def neutralize(text: str) -> str:
    """Make the fence delimiter unforgeable inside page text.

    A zero-width space is inserted after the ``<``, so the string no longer
    matches the delimiter while still reading identically to a human looking at
    the prompt. Done after :func:`strip_invisible`, or the marker we insert
    would be stripped straight back out.
    """
    return _DELIMITER.sub(lambda match: match.group(0).replace("<", "<​", 1), text)


def wrap_page_state(text: str) -> str:
    """Fence page content as untrusted data.

    Order matters: strip invisible characters first, then neutralize. The
    reverse would let a page hide a zero-width character inside its own
    ``</page_state>`` so that stripping reassembles a working delimiter after
    escaping had already run.
    """
    return f"{OPEN_TAG}\n{neutralize(strip_invisible(text))}\n{CLOSE_TAG}"
