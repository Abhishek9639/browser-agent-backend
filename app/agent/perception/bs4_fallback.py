"""Static HTML reading (spec section 4.6).

Used for the ``extract`` action and for pages where nothing needs clicking.
The JavaScript collector answers "what can I interact with"; this answers "what
does it say", which is a different question and a much cheaper one.

Masking follows the same rules as the collector, because a card number in a
table cell is exactly as sensitive as one in an input.
"""

import re
from typing import Any, Final

from bs4 import BeautifulSoup, Tag

from app.agent.perception.dom_extractor import MASK

__all__ = [
    "extract_list_items",
    "extract_main_text",
    "extract_tables",
    "parse",
]

# Never carries information the agent needs, and script bodies are a favourite
# place to park injected text.
_DROP_TAGS: Final = ("script", "style", "noscript", "template", "svg", "head")

_SECRET_HINT: Final = re.compile(
    r"(pass|pwd|card|cc[-_]?num|credit|cvv|cvc|csc|otp|pin|passcode|security[-_]?code|secret|token)",
    re.IGNORECASE,
)
# Long digit runs that look like a card. Spaced or hyphenated groups included,
# because that is how they are usually printed.
_CARD_LIKE: Final = re.compile(r"\b(?:\d[ -]?){13,19}\b")

_WHITESPACE: Final = re.compile(r"\s+")


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _is_hidden(tag: Tag) -> bool:
    """Best-effort hiding check.

    BeautifulSoup has no layout engine, so only inline styles and attributes
    can be seen; a class that hides via a stylesheet is invisible to us. That
    is why this path is the fallback: the collector, which runs in a real
    browser with computed styles, is the one that defends spec section 4.11.
    """
    if tag.has_attr("hidden"):
        return True
    if tag.get("aria-hidden") == "true":
        return True
    style = str(tag.get("style", "")).lower().replace(" ", "")
    return "display:none" in style or "visibility:hidden" in style or "opacity:0" in style


def _mask_text(text: str) -> str:
    """Redact anything shaped like a card number wherever it appears."""
    return _CARD_LIKE.sub(MASK, text)


def parse(html: str) -> BeautifulSoup:
    """Parse with lxml and drop the tags that never carry useful content."""
    soup = BeautifulSoup(html, "lxml")
    for name in _DROP_TAGS:
        for tag in soup.find_all(name):
            tag.decompose()
    for tag in soup.find_all(True):
        if _is_hidden(tag):
            tag.decompose()
    return soup


def extract_main_text(html: str) -> str:
    """The readable text of the page, collapsed and masked.

    Prefers ``main`` or ``article`` when present, because a page's chrome is
    rarely what an ``extract`` action is after.
    """
    soup = parse(html)
    root: Tag | BeautifulSoup = soup
    for selector in ("main", "article", "body"):
        found = soup.find(selector)
        if isinstance(found, Tag):
            root = found
            break

    for field in root.find_all("input"):
        field.replace_with(_input_placeholder(field))

    return _mask_text(_collapse(root.get_text(" ")))


def _input_placeholder(field: Tag) -> str:
    """Render an input as text, masking it when it looks sensitive."""
    value = str(field.get("value", ""))
    if not value:
        return ""
    hints = " ".join(str(field.get(attr, "")) for attr in ("type", "name", "id", "autocomplete"))
    if str(field.get("type", "")).lower() == "password" or _SECRET_HINT.search(hints):
        return MASK
    return value


def extract_tables(html: str) -> list[list[dict[str, str]]]:
    """Every table as a list of row dictionaries.

    Header cells become keys. A table without a header row falls back to
    positional keys, so a caller always gets dictionaries rather than having to
    branch on shape.
    """
    soup = parse(html)
    tables: list[list[dict[str, str]]] = []

    for table in soup.find_all("table"):
        rows = list(table.find_all("tr"))
        if not rows:
            continue

        headers = [_collapse(cell.get_text(" ")) for cell in rows[0].find_all(["th", "td"])]
        has_header = bool(rows[0].find_all("th"))
        body = rows[1:] if has_header else rows

        parsed: list[dict[str, str]] = []
        for row in body:
            cells = [
                _mask_text(_collapse(cell.get_text(" "))) for cell in row.find_all(["th", "td"])
            ]
            if not cells:
                continue
            keys = headers if has_header and len(headers) >= len(cells) else []
            parsed.append(
                {
                    (keys[position] if position < len(keys) else f"column_{position}"): value
                    for position, value in enumerate(cells)
                }
            )

        if parsed:
            tables.append(parsed)

    return tables


def extract_list_items(html: str) -> list[str]:
    """Text of every ``li``, collapsed and masked. Empty items are dropped."""
    soup = parse(html)
    items = [_mask_text(_collapse(item.get_text(" "))) for item in soup.find_all("li")]
    return [item for item in items if item]


def summarise_static(html: str) -> dict[str, Any]:
    """Everything the ``extract`` action might want, in one pass."""
    return {
        "text": extract_main_text(html),
        "tables": extract_tables(html),
        "list_items": extract_list_items(html),
    }
