"""Parse the PA Lottery print pages into ``Game`` records.

These pages are server-rendered HTML tables. Because the PA Lottery occasionally
tweaks column wording/order, we DON'T hard-code column positions. Instead we read
the header row and map each header cell to a canonical field using the synonym
lists below (case-insensitive substring match). To adapt to a wording change you
usually only add one synonym here — no other code changes.

If a page can't be mapped to the columns we need, parsing raises ``ParseError``
with the headers it actually saw, so the failure is obvious instead of silent.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from .model import Game


class ParseError(RuntimeError):
    pass


# Canonical field -> list of header substrings that indicate that field.
# Order matters only in that the first canonical field whose synonym matches wins
# for a given header cell.
_SYNONYMS: dict[str, list[str]] = {
    "game_number": ["game number", "game no", "game #", "game num", "number"],
    "name": ["game name", "name", "title", "game"],
    "price": ["price", "ticket price", "cost"],
    "sales_end_date": ["sales end", "end sale", "last day to sell", "sale end", "end date"],
    "claim_deadline": ["claim", "redeem", "last day to claim", "expiration", "expire"],
    "top_prize_value": ["top prize", "grand prize", "prize amount", "prize value"],
    "top_prizes_total": ["total top prizes", "top prizes printed", "original top", "top prizes total"],
    "top_prizes_remaining": ["top prizes remaining", "top prizes left", "top prizes unclaimed", "remaining top"],
    "total_prizes_remaining": ["prizes remaining", "prizes left", "total prizes remaining", "unclaimed prizes"],
    "total_prizes_original": ["total prizes", "prizes printed", "original prizes", "prizes total"],
}

_INT_RE = re.compile(r"-?\d[\d,]*")
_MONEY_RE = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)")
_GAMENO_RE = re.compile(r"\b(\d{3,5})\b")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _to_int(text: str) -> int | None:
    if text is None:
        return None
    m = _INT_RE.search(text.replace(" ", " "))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _to_price(text: str) -> float | None:
    if not text:
        return None
    m = _MONEY_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _map_header(label: str) -> str | None:
    """Return the canonical field for a header label, or None if unrecognized.

    More-specific synonyms (e.g. "top prizes remaining") are checked before the
    looser ones ("prizes remaining") because the dict is ordered that way and we
    return on the first hit per canonical field, longest-synonym-first.
    """
    low = _clean(label).lower()
    best: tuple[int, str] | None = None  # (synonym length, field)
    for field, syns in _SYNONYMS.items():
        for syn in syns:
            if syn in low:
                if best is None or len(syn) > best[0]:
                    best = (len(syn), field)
    return best[1] if best else None


def _header_cells(table) -> list[str]:
    head = table.find("thead")
    if head:
        row = head.find("tr")
    else:
        row = table.find("tr")
    if not row:
        return []
    return [_clean(c.get_text()) for c in row.find_all(["th", "td"])]


def _data_rows(table):
    body = table.find("tbody") or table
    rows = body.find_all("tr")
    # Drop the header row if there's no <thead>/<tbody> separation.
    if not table.find("thead"):
        rows = rows[1:]
    return rows


def _extract_game_number(cells_text: list[str], row) -> str | None:
    # Prefer a dedicated game-number column value; fall back to a link href or
    # any standalone 3-5 digit token in the row.
    for a in row.find_all("a", href=True):
        m = re.search(r"(?:id|game|number)=(\d{3,5})", a["href"], re.I)
        if m:
            return m.group(1)
    for txt in cells_text:
        m = _GAMENO_RE.fullmatch(_clean(txt))
        if m:
            return m.group(1)
    return None


def parse_table(html: str, *, required: set[str], status: str) -> list[Game]:
    """Parse the first table whose headers cover ``required`` canonical fields."""
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        raise ParseError("No <table> elements found on the page.")

    best_table = None
    best_mapping: dict[int, str] = {}
    best_score = -1
    seen_headers: list[list[str]] = []

    for table in tables:
        headers = _header_cells(table)
        if not headers:
            continue
        seen_headers.append(headers)
        mapping = {}
        for idx, label in enumerate(headers):
            field = _map_header(label)
            if field and field not in mapping.values():
                mapping[idx] = field
        score = len(required & set(mapping.values()))
        if score > best_score:
            best_score, best_table, best_mapping = score, table, mapping

    if best_table is None or not (required <= set(best_mapping.values())):
        raise ParseError(
            f"Could not find a table with required columns {sorted(required)}.\n"
            f"Headers seen on the page: {seen_headers}\n"
            "Add the page's wording to _SYNONYMS in parse.py to fix this."
        )

    games: list[Game] = []
    for row in _data_rows(best_table):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        cells_text = [_clean(c.get_text()) for c in cells]
        values: dict[str, str] = {}
        for idx, field in best_mapping.items():
            if idx < len(cells_text):
                values[field] = cells_text[idx]

        game_number = values.get("game_number") or _extract_game_number(cells_text, row)
        if not game_number:
            continue  # skip subtotal / blank rows
        game_number = _clean(game_number)
        # If the number column held extra text, keep just the digits.
        m = _GAMENO_RE.search(game_number)
        if m:
            game_number = m.group(1)

        g = Game(game_number=game_number, status=status, source_pages=[status_to_page(status)])
        g.name = values.get("name", "")
        g.price = _to_price(values.get("price", ""))
        g.sales_end_date = values.get("sales_end_date") or None
        g.claim_deadline = values.get("claim_deadline") or None
        g.top_prize_value = values.get("top_prize_value") or None
        g.top_prizes_total = _to_int(values.get("top_prizes_total", ""))
        g.top_prizes_remaining = _to_int(values.get("top_prizes_remaining", ""))
        g.total_prizes_remaining = _to_int(values.get("total_prizes_remaining", ""))
        g.total_prizes_original = _to_int(values.get("total_prizes_original", ""))
        games.append(g)

    return games


def status_to_page(status: str) -> str:
    return "sales_ended" if status == "ended" else "remaining"


def parse_sales_ended(html: str) -> list[Game]:
    # Game number is the only thing we strictly need to identify an ended game.
    return parse_table(html, required={"game_number"}, status="ended")


def parse_remaining(html: str) -> list[Game]:
    return parse_table(html, required={"game_number"}, status="active")


def parse_active(html: str) -> list[Game]:
    """The ActivePrint page: the authoritative list of games still on sale."""
    return parse_table(html, required={"game_number"}, status="active")
