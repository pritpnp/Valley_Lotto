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
# Used by the generic table parser for the Active and Sales-Ended pages. The
# Remaining page has its own dedicated parser (multi-value columns) below.
_SYNONYMS: dict[str, list[str]] = {
    "game_number": ["game number", "game no", "game #", "game num", "number"],
    "name": ["game name", "name", "title", "game"],
    "price": ["price", "ticket price", "cost"],
    "sales_end_date": ["sales end", "end sale", "last day to sell", "sale end", "end date"],
    "claim_deadline": ["claim", "redeem", "last day to claim", "expiration", "expire"],
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
        g.name = clean_name(values.get("name", ""))
        g.price = _to_price(values.get("price", ""))
        g.sales_end_date = values.get("sales_end_date") or None
        g.claim_deadline = values.get("claim_deadline") or None
        games.append(g)

    return games


def status_to_page(status: str) -> str:
    return "sales_ended" if status == "ended" else "remaining"


# PA decorates game names with a "NEW " prefix and an " Available on iLottery"
# suffix; strip them so names match across pages and read cleanly in reports.
_NAME_PREFIX_RE = re.compile(r"^\s*NEW\s+", re.I)
_NAME_SUFFIX_RE = re.compile(r"\s*Available on iLottery\.?\s*$", re.I)


def clean_name(name: str) -> str:
    name = _clean(name)
    name = _NAME_PREFIX_RE.sub("", name)
    name = _NAME_SUFFIX_RE.sub("", name)
    return name.strip()


def parse_sales_ended(html: str) -> list[Game]:
    # Game number is the only thing we strictly need to identify an ended game.
    return parse_table(html, required={"game_number"}, status="ended")


def parse_active(html: str) -> list[Game]:
    """The ActivePrint page: the authoritative list of games still on sale."""
    return parse_table(html, required={"game_number"}, status="active")


# --------------------------------------------------------------------------- #
# Remaining page — dedicated parser
# --------------------------------------------------------------------------- #
# The "Remaining" page is special: two of its columns hold SIX space-separated
# values each — "Top Six Prizes" (the six highest prize amounts) and
# "Wins Remaining" (how many of each are still unclaimed). tier 1 = the top prize.
_TOP_PRIZES_HDR = ("top six prizes", "top prizes", "prize")
_WINS_HDR = ("wins remaining", "prizes remaining", "remaining")
_MONEY_TOKEN_RE = re.compile(r"\$[\d,]+(?:\.\d+)?")


def _find_col(headers: list[str], needles: tuple[str, ...]) -> int | None:
    low = [h.lower() for h in headers]
    for needle in needles:  # most-specific needle first
        for i, h in enumerate(low):
            if needle in h:
                return i
    return None


_DETAIL_TOP_RE = re.compile(r"offers?\s+([\d,]+)\s+[Tt]op\s+[Pp]rize", re.I)
_DETAIL_ODDS_RE = re.compile(r"chances of winning a prize:\s*1:([\d.]+)", re.I)


def parse_detail(html: str) -> dict:
    """Pull the original top-prize count and overall odds from a game's detail page.

    PA states it in prose, e.g. "...is a $2 game that offers 7 Top Prizes of
    $17,000." and "Overall chances of winning a prize: 1:3.38". Returns
    {"top_prizes_original": int|None, "odds": str|None}.
    """
    text = " ".join(BeautifulSoup(html, "html.parser").get_text(" ").split())
    top = _DETAIL_TOP_RE.search(text)
    odds = _DETAIL_ODDS_RE.search(text)
    return {
        "top_prizes_original": int(top.group(1).replace(",", "")) if top else None,
        "odds": f"1:{odds.group(1)}" if odds else None,
    }


def parse_remaining(html: str) -> list[Game]:
    """Parse PA's prizes-remaining table into Games with per-tier prize counts."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise ParseError("No <table> on the Remaining page.")

    headers = _header_cells(table)
    col_num = _find_col(headers, ("game #", "game number", "game no", "number"))
    col_name = _find_col(headers, ("game name", "name"))
    col_price = _find_col(headers, ("price", "cost"))
    col_prizes = _find_col(headers, _TOP_PRIZES_HDR)
    col_wins = _find_col(headers, _WINS_HDR)

    if col_num is None or col_prizes is None or col_wins is None:
        raise ParseError(
            "Remaining page is missing expected columns.\n"
            f"Headers seen: {headers}\n"
            "Update parse_remaining()/_TOP_PRIZES_HDR/_WINS_HDR in parse.py."
        )

    games: list[Game] = []
    for row in _data_rows(table):
        cells = [_clean(c.get_text()) for c in row.find_all(["td", "th"])]
        if not cells or col_num >= len(cells):
            continue
        m = _GAMENO_RE.search(cells[col_num])
        if not m:
            continue
        g = Game(game_number=m.group(1), status="active", source_pages=["remaining"])
        # Capture the detail-page id from the row's link, so we can later look up
        # the original prize counts.
        for a in row.find_all("a", href=True):
            dm = re.search(r"[?&]id=(\d+)", a["href"])
            if dm:
                g.detail_id = dm.group(1)
                break
        if col_name is not None and col_name < len(cells):
            g.name = clean_name(cells[col_name])
        if col_price is not None and col_price < len(cells):
            g.price = _to_price(cells[col_price])

        prize_vals = _MONEY_TOKEN_RE.findall(cells[col_prizes]) if col_prizes < len(cells) else []
        win_counts = [
            int(t.replace(",", ""))
            for t in re.findall(r"\d[\d,]*", cells[col_wins])
        ] if col_wins < len(cells) else []

        tiers = []
        for i, val in enumerate(prize_vals):
            tiers.append({"value": val, "remaining": win_counts[i] if i < len(win_counts) else None})
        g.prize_tiers = tiers
        if tiers:
            g.top_prize_value = tiers[0]["value"]
            g.top_prizes_remaining = tiers[0]["remaining"]
        games.append(g)

    return games
