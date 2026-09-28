"""Task signatures (spec section 4.12).

``sha256(normalized_instruction + start_domain)``. The whole value of the
cache rests on two tasks that mean the same thing producing the same
signature, and two tasks that mean different things never doing so, so the
normalisation is deliberately conservative: it removes only differences that
cannot change what the user asked for.

What is normalised away: case, surrounding and repeated whitespace, unicode
that has more than one encoding, and trailing punctuation. What is not:
anything inside the sentence. "flights to Delhi" and "flights from Delhi" are
different tasks and must stay different, which rules out stemming, stop-word
removal and every other trick that would raise the hit rate by occasionally
replaying the wrong flow.
"""

import hashlib
import re
import unicodedata
from typing import Final
from urllib.parse import urlsplit

__all__ = ["normalize", "start_domain", "task_signature"]

_WHITESPACE: Final = re.compile(r"\s+")

# Trailing punctuation only. A question mark at the end of "find the cheapest
# flight?" carries no meaning; a full stop in "Rs 4,299." does.
# Includes the typographic dashes and quotes a phone keyboard produces.
_TRAILING: Final = ".,;:!?- \t\n\"'" + "\u2013\u2014\u201d\u2019"


def normalize(instruction: str) -> str:
    """The comparable form of an instruction.

    NFKC first, because a non-breaking space and an ordinary one look
    identical to a user and are different bytes, as are a full-width digit and
    an ASCII one. An instruction typed on a phone keyboard and the same one
    pasted from a web page should not miss each other over that.
    """
    folded = unicodedata.normalize("NFKC", instruction).casefold()
    collapsed = _WHITESPACE.sub(" ", folded).strip()
    return collapsed.rstrip(_TRAILING).strip()


def start_domain(start_url: str | None) -> str:
    """The host a task starts on, or an empty string.

    The port is deliberately excluded. A demo site on a random port and the
    same site on another are the same site for caching purposes, and a suite
    that binds a fresh port per run would otherwise never hit its own cache.

    An empty string is a real answer, not a failure: a task with no start URL
    is keyed on its instruction alone, which is the best that can be said
    about it.
    """
    if not start_url:
        return ""
    return (urlsplit(start_url).hostname or "").casefold()


def task_signature(instruction: str, start_url: str | None = None) -> str:
    """The cache key. 64 hex characters, which is what the column holds.

    The two parts are joined with a null byte rather than concatenated, so an
    instruction ending in a hostname cannot collide with a shorter one that
    started on that host.
    """
    material = f"{normalize(instruction)}\x00{start_domain(start_url)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
