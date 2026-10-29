"""Read the draw-game winning numbers: PA's own feed, and the old scraper's file.

PA's feed (``game-number-feed.aspx``) is one small XML document with the latest
draw of every draw game, in the order PA lists them::

    <game id="12"><title>Powerball</title><drawdate>10/26/2026</drawdate>
      <numbers>18 26 49 50 54 PB: 01 PP: 03</numbers>
      <jackpotdate>10/28/2026</jackpotdate><jackpotamount>$716,000,000</jackpotamount>
      <cashvalue>$307,200,000</cashvalue></game>

The old scraper (``pritpnp/powerball-megamillion``) writes ``lottery.json`` with
just Powerball and Mega Millions. Both are read into the same :class:`Draw`, so
the two can be saved side by side and compared.

Nothing is guessed. A game whose numbers or draw date can't be read exactly is
left out, and the reason is noted in plain words so it can be reported.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from xml.etree.ElementTree import tostring

try:  # safer XML parsing when it's installed; the standard library otherwise
    from defusedxml.ElementTree import fromstring  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — depends on what's installed
    from xml.etree.ElementTree import fromstring

PA_FEED = "pa_feed"
OLD_SCRAPER = "old_scraper"

# The extra balls PA writes after the main numbers, by the label it uses.
EXTRA_NAMES = {
    "WB": "Wild Ball",
    "PB": "Powerball",
    "PP": "Power Play",
    "MB": "Mega Ball",
    "Mill Ball": "Millionaire Ball",
}

# The two games the old scraper covers, as PA's feed numbers them.
POWERBALL, MEGA_MILLIONS = "12", "15"
OLD_GAMES = {
    "powerball": (POWERBALL, "Powerball", "PB"),
    "megaMillions": (MEGA_MILLIONS, "Mega Millions", "MB"),
}


class FeedError(ValueError):
    """The whole document can't be read: nothing from it is used."""


@dataclass
class Draw:
    """One draw of one game, as one source published it."""

    game_id: str
    title: str
    draw_date: str                         # YYYY-MM-DD
    numbers: list[str]                     # as published: "05" stays "05"
    extras: list[dict] = field(default_factory=list)   # [{"code","label","value"}]
    jackpot_date: str | None = None        # the next draw, YYYY-MM-DD
    jackpot_text: str | None = None        # as published
    jackpot_amount: float | None = None    # only when it's plain dollars
    cash_value_text: str | None = None
    cash_value_amount: float | None = None
    raw: str = ""                          # the entry exactly as read
    position: int | None = None            # place in PA's feed (0 = first)
    source: str = PA_FEED

    def extra(self, code: str) -> str | None:
        """The value of one extra ball, e.g. ``extra("PB") == "01"``."""
        for e in self.extras:
            if e.get("code") == code:
                return e.get("value")
        return None


@dataclass
class Reading:
    """What one read of a source gave: the draws, and anything worth a look.

    ``complete`` is False when something (a game, or one of its values) had to
    be left out because it couldn't be read exactly. A new extra-ball label is
    a problem worth reporting, but nothing is left out for it.
    """

    draws: list[Draw]
    problems: list[str] = field(default_factory=list)
    complete: bool = True
    order: list[str] = field(default_factory=list)   # every game id, as listed


# --------------------------------------------------------------------------- #
# Small strict readers shared by both sources
# --------------------------------------------------------------------------- #

_MONEY = re.compile(r"\$(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?")
_MILLIONS = re.compile(r"\$(\d+(?:\.\d+)?)\s+(million|billion)", re.IGNORECASE)
_SLASH_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def money_amount(text: str | None) -> float | None:
    """``"$716,000,000"`` -> 716000000.0. Anything else (``"$1,000,000 A Year
    For Life"``, ``"$1,00,000"``, ``"716000000"``) -> None: it isn't a plain amount."""
    m = _MONEY.fullmatch((text or "").strip())
    if not m:
        return None
    return float(Decimal(m.group(1).replace(",", "") + "." + (m.group(2) or "0")))


def millions_amount(text: str | None) -> float | None:
    """The old scraper's ``"$463 Million"`` -> 463000000.0 (Million or Billion
    only; anything else -> None)."""
    m = _MILLIONS.fullmatch((text or "").strip())
    if not m:
        return None
    try:
        scale = 10 ** 6 if m.group(2).lower() == "million" else 10 ** 9
        return float(Decimal(m.group(1)) * scale)
    except InvalidOperation:  # pragma: no cover — the pattern only lets digits through
        return None


def slash_date(text: str | None) -> str | None:
    """``"10/26/2026"`` (month/day/year) -> ``"2026-10-26"``, or None."""
    m = _SLASH_DATE.fullmatch((text or "").strip())
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
    except ValueError:            # 02/30/2026
        return None


_MAIN_NUMBER = re.compile(r"\d{1,2}")
# One "LABEL: value" pair; the label may hold spaces ("Mill Ball: 03").
_EXTRA = re.compile(r"\s*([A-Za-z][A-Za-z ]*?)\s*:\s*(\d{1,2})(?=\s|$)")


def split_numbers(text: str) -> tuple[list[str], list[dict], list[str]]:
    """``"18 26 49 50 54 PB: 01 PP: 03"`` -> main numbers, extras, new labels.

    Raises ValueError if the text isn't exactly main numbers followed by
    ``LABEL: value`` pairs. A label we don't know yet is kept (its name is the
    label itself) and returned in the third list so it can be reported.
    """
    text = " ".join((text or "").split())
    main: list[str] = []
    rest = text
    while rest:
        token, _, after = rest.partition(" ")
        if not _MAIN_NUMBER.fullmatch(token):
            break
        main.append(token)
        rest = after
    if not main:
        raise ValueError(f"no winning numbers in “{text}”")
    extras: list[dict] = []
    unknown: list[str] = []
    pos = 0
    while pos < len(rest):
        m = _EXTRA.match(rest, pos)
        if not m:
            raise ValueError(f"can't read “{rest[pos:].strip()}” in “{text}”")
        code, value = m.group(1).strip(), m.group(2)
        if any(e["code"] == code for e in extras):
            raise ValueError(f"“{code}” appears twice in “{text}”")
        if code not in EXTRA_NAMES:
            unknown.append(code)
        extras.append({"code": code, "label": EXTRA_NAMES.get(code, code), "value": value})
        pos = m.end()
    return main, extras, unknown


# --------------------------------------------------------------------------- #
# PA's feed
# --------------------------------------------------------------------------- #

def _text(game, tag: str) -> str | None:
    """An element's text, or None when it's missing or empty (``<jackpotdate />``)."""
    el = game.find(tag)
    if el is None or el.text is None or not el.text.strip():
        return None
    return " ".join(el.text.split())


def _raw(el) -> str:
    tail, el.tail = el.tail, None          # just the <game>, not what follows it
    try:
        return tostring(el, encoding="unicode")
    finally:
        el.tail = tail


def read_feed(xml_text: str | bytes) -> Reading:
    """Every game in PA's feed that reads exactly, in the feed's order, plus a
    note for each game left out and each new extra-ball label.

    Raises :class:`FeedError` when the document itself can't be read (not XML,
    not a list of games, or no games at all).
    """
    if isinstance(xml_text, str):
        xml_text = xml_text.lstrip("\ufeff")   # a BOM left in decoded text
    else:
        xml_text = xml_text.lstrip(b"\xef\xbb\xbf")
    try:
        root = fromstring(xml_text)
    except Exception as e:  # noqa: BLE001 — any parse failure means "not readable"
        raise FeedError(f"PA's number feed isn't readable XML any more ({type(e).__name__}).") from e
    if root.tag != "games":
        raise FeedError(f"PA's number feed changed shape: it starts with <{root.tag}>, not <games>.")
    games = root.findall("game")
    if not games:
        raise FeedError("PA's number feed has no games in it.")

    draws: list[Draw] = []
    problems: list[str] = []
    complete = True
    order: list[str] = []
    for position, el in enumerate(games):
        game_id = (el.get("id") or "").strip()
        if game_id and game_id not in order:
            order.append(game_id)
        title = _text(el, "title")
        name = title or (f"game {game_id}" if game_id else f"entry {position + 1}")
        if not game_id or not title:
            problems.append(f"{name}: it has no {'id' if not game_id else 'name'}; left out.")
            complete = False
            continue
        draw_date = slash_date(_text(el, "drawdate"))
        if draw_date is None:
            problems.append(f"{name}: can't read the draw date “{_text(el, 'drawdate') or ''}”; left out.")
            complete = False
            continue
        try:
            main, extras, unknown = split_numbers(_text(el, "numbers") or "")
        except ValueError as e:
            problems.append(f"{name}: {e}; left out.")
            complete = False
            continue
        for code in unknown:
            problems.append(f"{name}: a new kind of extra ball, “{code}”. "
                            f"It's shown as “{code}” until it's given a name.")
        jackpot_date = None
        if _text(el, "jackpotdate") is not None:
            jackpot_date = slash_date(_text(el, "jackpotdate"))
            if jackpot_date is None:
                problems.append(f"{name}: can't read the next draw date "
                                f"“{_text(el, 'jackpotdate')}”; left out.")
                complete = False
        jackpot_text = _text(el, "jackpotamount")
        cash_text = _text(el, "cashvalue")
        draws.append(Draw(
            game_id=game_id, title=title, draw_date=draw_date,
            numbers=main, extras=extras,
            jackpot_date=jackpot_date,
            jackpot_text=jackpot_text, jackpot_amount=money_amount(jackpot_text),
            cash_value_text=cash_text, cash_value_amount=money_amount(cash_text),
            raw=_raw(el), position=position, source=PA_FEED,
        ))
    ids = [d.game_id for d in draws]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        # Two different entries for one game: which one is right can't be told.
        problems.append(f"game {', '.join(dupes)} is listed twice; left out.")
        draws = [d for d in draws if d.game_id not in dupes]
        complete = False
    return Reading(draws, problems, complete, order)


def parse_feed(xml_text: str | bytes) -> list[Draw]:
    """PA's feed -> one :class:`Draw` per game, in the feed's order.

    Strict: raises :class:`FeedError` if any game can't be read exactly. (The
    scheduled job uses :func:`read_feed`, which keeps the games that do read
    and reports the rest.) A new extra-ball label is not an error: it's kept.
    """
    reading = read_feed(xml_text)
    if not reading.complete:
        raise FeedError("PA's number feed: " + " ".join(reading.problems))
    return reading.draws


# --------------------------------------------------------------------------- #
# The old scraper's lottery.json
# --------------------------------------------------------------------------- #

def _long_date(text: str, fmt: str) -> str | None:
    try:
        return datetime.strptime(text.strip(), fmt).date().isoformat()
    except (ValueError, AttributeError):
        return None


def _drawing_date(text) -> str | None:
    """``"October 27, 2026 Drawing"`` -> ``"2026-10-27"``."""
    if not isinstance(text, str) or not text.strip().endswith(" Drawing"):
        return None
    return _long_date(text.strip()[: -len(" Drawing")], "%B %d, %Y")


def _next_drawing(text) -> str | None:
    """``"Friday, October 30, 2026"`` -> ``"2026-10-30"`` (the weekday must agree)."""
    if not isinstance(text, str):
        return None
    iso = _long_date(text, "%A, %B %d, %Y")
    if iso is None:
        return None
    weekday = text.split(",", 1)[0].strip().lower()
    return iso if date.fromisoformat(iso).strftime("%A").lower() == weekday else None


def read_old_scraper(json_text: str | bytes) -> Reading:
    """Powerball (12) and Mega Millions (15) from the old scraper's file.

    ``numbers`` is five main numbers then the bonus ball. Numbers are written
    two digits wide, like PA's feed (3 -> "03").
    """
    if isinstance(json_text, str):
        json_text = json_text.lstrip("\ufeff")
    try:
        data = json.loads(json_text)
    except ValueError as e:
        raise FeedError("The old scraper's lottery.json isn't readable JSON any more.") from e
    if not isinstance(data, dict):
        raise FeedError("The old scraper's lottery.json changed shape.")
    draws: list[Draw] = []
    problems: list[str] = []
    complete = True
    for key, (game_id, title, bonus) in OLD_GAMES.items():
        entry = data.get(key)
        if not isinstance(entry, dict):
            problems.append(f"{title}: missing from the old scraper's file.")
            complete = False
            continue
        nums = entry.get("numbers")
        if (not isinstance(nums, list) or len(nums) != 6
                or not all(type(n) is int and 1 <= n <= 99 for n in nums)):
            problems.append(f"{title}: the old scraper's numbers {nums!r} aren't "
                            f"five numbers and a bonus ball; left out.")
            complete = False
            continue
        draw_date = _drawing_date(entry.get("date"))
        if draw_date is None:
            problems.append(f"{title}: can't read the old scraper's draw date "
                            f"{entry.get('date')!r}; left out.")
            complete = False
            continue
        next_date = None
        if entry.get("nextDrawing"):
            next_date = _next_drawing(entry.get("nextDrawing"))
            if next_date is None:
                problems.append(f"{title}: can't read the old scraper's next draw "
                                f"{entry.get('nextDrawing')!r}; left out.")
                complete = False
        jackpot = entry.get("nextJackpot")
        jackpot = " ".join(jackpot.split()) if isinstance(jackpot, str) and jackpot.strip() else None
        draws.append(Draw(
            game_id=game_id, title=title, draw_date=draw_date,
            numbers=[f"{n:02d}" for n in nums[:5]],
            extras=[{"code": bonus, "label": EXTRA_NAMES[bonus], "value": f"{nums[5]:02d}"}],
            jackpot_date=next_date,
            jackpot_text=jackpot, jackpot_amount=millions_amount(jackpot),
            raw=json.dumps(entry, sort_keys=True), position=None, source=OLD_SCRAPER,
        ))
    return Reading(draws, problems, complete)


def parse_old_scraper(json_text: str | bytes) -> list[Draw]:
    """The old scraper's ``lottery.json`` -> Powerball and Mega Millions draws.

    Strict, like :func:`parse_feed`: raises :class:`FeedError` if either game
    can't be read exactly.
    """
    reading = read_old_scraper(json_text)
    if not reading.complete:
        raise FeedError("The old scraper's lottery.json: " + " ".join(reading.problems))
    return reading.draws
