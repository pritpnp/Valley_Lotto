"""PA's weekly "Scratch-Offs Available for Ordering" list.

PA links a new PDF from its retailer page every week (the link's file name
changes, e.g. ``Scratch-Offs_Order_10-26-26.pdf``). The PDF is one table::

    As of
    10/26/26 Price # Tix/Pack GAMES AVAILABLE TO ORDER
    1806     $1    100        CASH GRAB
    ...

It's what the app uses for real tickets-per-pack and for which games can be
ordered at all. The list is read whole or not at all: if any row isn't exactly
game number / price / tickets per pack / name, nothing from it is used.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin

from .draws import money_amount

LINK_TEXT = "scratch-offs available for ordering"


class OrderListError(ValueError):
    """The retailer page or the PDF isn't in the shape this reads."""


@dataclass
class OrderGame:
    game_number: str
    price: float
    tickets_per_pack: int
    name: str


@dataclass
class OrderList:
    as_of: str                              # YYYY-MM-DD, from the PDF
    games: list[OrderGame] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# The link on the retailer page
# --------------------------------------------------------------------------- #

def find_order_link(html: str | bytes, base_url: str) -> str:
    """The full URL of the "Scratch-Offs Available for Ordering" link.

    Matched on the link's words (spaces and ``&nbsp;`` collapsed, any case),
    never on where it sits on the page.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    found = []
    for a in soup.find_all("a", href=True):
        words = " ".join(a.get_text(" ").split()).casefold()
        if words == LINK_TEXT:
            url = urljoin(base_url, a["href"].strip())
            if url not in found:
                found.append(url)
    if not found:
        raise OrderListError("PA's retailer page no longer has a “Scratch-Offs Available "
                             "for Ordering” link.")
    if len(found) > 1:
        raise OrderListError("PA's retailer page has more than one “Scratch-Offs Available "
                             "for Ordering” link: " + ", ".join(found))
    return found[0]


_URL_DATE = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{2}|\d{4})\.pdf$", re.IGNORECASE)


def url_date(url: str) -> str | None:
    """The date in the link's file name (``..._10-26-26.pdf`` -> ``"2026-10-26"``)."""
    m = _URL_DATE.search(url.split("?", 1)[0])
    return _ymd(m.group(1), m.group(2), m.group(3)) if m else None


def _ymd(month: str, day: str, year: str) -> str | None:
    y = int(year) + (2000 if len(year) == 2 else 0)
    try:
        return date(y, int(month), int(day)).isoformat()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# The PDF
# --------------------------------------------------------------------------- #

_AS_OF = re.compile(r"\bAs\s+of\s*:?\s*(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?!\d)", re.IGNORECASE)
_ROW = re.compile(r"(\d+)\s+(\$\S+)\s+(\d+)\s+(\S.*)")
_HEADINGS = ("Price", "Tix/Pack", "GAMES AVAILABLE TO ORDER")
# The database's column sizes (``OrderGameRow`` in web/models.py). Postgres
# refuses anything longer, so a row like that isn't one this list can hold.
LONGEST_NUMBER, LONGEST_NAME = 16, 255
# The whole heading line, word for word (the "As of" date shares its line). A
# column added between these would still give rows that read as game number,
# price, tickets per pack and name, with the new column glued onto the name,
# so any other wording on the line means the list has changed.
_HEADING_LINE = re.compile(r"(?:(?:As\s+of\s*:?\s*)?\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})\s+)?"
                           r"Price\s+(?:#\s*)?Tix/Pack\s+GAMES\s+AVAILABLE\s+TO\s+ORDER",
                           re.IGNORECASE)


def _heading_at(line: str) -> list[int]:
    """Where each heading starts in ``line`` (-1 when it isn't there)."""
    upper = line.upper()
    return [upper.find(h.upper()) for h in _HEADINGS]


def _pdf_pages(pdf_bytes: bytes) -> list[str]:
    import pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return [page.extract_text() or "" for page in pdf.pages]
    except Exception as e:  # noqa: BLE001 — any failure here means "not a readable PDF"
        raise OrderListError(f"PA's order list isn't a readable PDF ({type(e).__name__}).") from e


def parse_order_pdf(pdf_bytes: bytes) -> OrderList:
    """The PDF -> :class:`OrderList`, or :class:`OrderListError` with the reason."""
    pages = _pdf_pages(pdf_bytes)
    text = "\n".join(pages)

    dates = {_ymd(*m.groups()) for m in _AS_OF.finditer(text)}
    if not dates:
        raise OrderListError("PA's order list has no “As of” date.")
    if None in dates or len(dates) > 1:
        raise OrderListError("PA's order list has an “As of” date that can't be read, "
                             "or more than one.")
    as_of = dates.pop()

    games: list[OrderGame] = []
    heading_seen = False
    for line in text.splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        m = _ROW.fullmatch(line)
        if m:                            # checked first: a game may be called "THE PRICE IS RIGHT"
            price = money_amount(m.group(2))
            if not heading_seen:
                raise OrderListError("PA's order list has games above its heading.")
            if (price is None or int(m.group(3)) <= 0 or len(m.group(1)) > LONGEST_NUMBER
                    or len(m.group(4)) > LONGEST_NAME):
                raise OrderListError(f"PA's order list has a line that isn't game number, "
                                     f"price, tickets per pack and name: “{line}”.")
            games.append(OrderGame(game_number=m.group(1), price=price,
                                   tickets_per_pack=int(m.group(3)), name=m.group(4)))
            continue
        at = _heading_at(line)
        if any(i >= 0 for i in at):
            # The heading: all three, in this order, and nothing else, or the
            # columns have changed.
            if not _HEADING_LINE.fullmatch(line):
                raise OrderListError(f"PA's order list heading changed: “{line}” "
                                     f"(expected Price, Tix/Pack, GAMES AVAILABLE TO ORDER).")
            heading_seen = True
            continue
        if _AS_OF.fullmatch(line) or line.casefold() == "as of":
            continue
        if heading_seen:
            raise OrderListError(f"PA's order list has a line that isn't game number, "
                                 f"price, tickets per pack and name: “{line}”.")
        # Anything else above the heading is a title; the heading is what counts.
    if not heading_seen:
        raise OrderListError("PA's order list heading changed: Price, Tix/Pack and "
                             "GAMES AVAILABLE TO ORDER weren't found.")
    if not games:
        raise OrderListError("PA's order list has no games on it.")
    numbers = [g.game_number for g in games]
    twice = sorted({x for x in numbers if numbers.count(x) > 1})
    if twice:
        raise OrderListError(f"PA's order list has game {', '.join(twice)} more than once.")
    return OrderList(as_of=as_of, games=games)


# --------------------------------------------------------------------------- #
# Cross-checks (worth a look, but the list is still used)
# --------------------------------------------------------------------------- #

def cross_check(order_list: OrderList, url: str, known: dict | None = None) -> list[str]:
    """Things that don't add up, in plain words. ``known`` is the tracker's games
    (``data/state.json``, game number -> game with ``.price`` and ``.name``)."""
    notes = []
    linked = url_date(url)
    if linked and linked != order_list.as_of:
        notes.append(f"The link is dated {linked} but the list inside says as of "
                     f"{order_list.as_of}.")
    if known:
        for g in order_list.games:
            k = known.get(g.game_number)
            if k is None:
                continue
            if k.price is not None and float(k.price) != g.price:
                notes.append(f"#{g.game_number} {g.name}: ${g.price:g} on the order list, "
                             f"${float(k.price):g} on PA's game pages.")
        new = [g for g in order_list.games if g.game_number not in known]
        if new:
            notes.append("Not on PA's game pages yet: " + ", ".join(
                f"#{g.game_number} {g.name} (${g.price:g})" for g in new) + ".")
    return notes
