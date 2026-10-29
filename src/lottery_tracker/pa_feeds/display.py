"""The TV feed: every draw game's latest numbers, ready for the store TVs.

The app serves this at ``/api/numbers`` (no login: it's public, and never has
store data in it). One entry per game, its latest draw, in PA's own order.

Powerball and Mega Millions come from the old scraper until PA's feed has
matched it for the trial (see ``store.compare_and_switch``), then from PA's
feed. While the old scraper is in charge, PA's extras it doesn't have (Power
Play, cash value) are added only when PA's reading of that same draw matched
it. (If the old scraper has never sent a game at all, PA's reading is shown
rather than nothing; ``source`` says which one it is.)

After the switch there's one fallback: if the old scraper has a newer draw of
Powerball or Mega Millions than PA's feed (PA has fallen behind and the owner
turned the old scraper back on), that draw is shown until PA catches up.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import and_, func, select

from ..web.models import DrawCheckRow, DrawResultRow
from .draws import OLD_SCRAPER, PA_FEED
from .store import PBMM, aware, feed_order, pbmm_source, utcnow


def _stamp(dt: datetime | None) -> str | None:
    """``2026-10-29T00:20:00Z``."""
    if dt is None:
        return None
    return aware(dt).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dollars(text: str | None, amount: float | None) -> dict | None:
    """``{"text": "$716,000,000", "amount": 716000000}``, or None when empty."""
    if not text:
        return None
    if amount is not None and float(amount).is_integer():
        amount = int(amount)
    return {"text": text, "amount": amount}


def _latest(session, source: str) -> dict[str, DrawResultRow]:
    """Each game's most recent draw from one source."""
    newest = (select(DrawResultRow.game_id, func.max(DrawResultRow.draw_date).label("d"))
              .where(DrawResultRow.source == source)
              .group_by(DrawResultRow.game_id).subquery())
    rows = session.scalars(select(DrawResultRow).join(newest, and_(
        DrawResultRow.source == source,
        DrawResultRow.game_id == newest.c.game_id,
        DrawResultRow.draw_date == newest.c.d)))
    return {r.game_id: r for r in rows}


def _entry(row: DrawResultRow, position: int, title: str | None = None) -> dict:
    return {
        "id": row.game_id,
        "title": title or row.title,
        "position": position,
        "draw_date": row.draw_date,
        "numbers": row.numbers.split(),
        "extras": json.loads(row.extras or "[]"),
        "next_draw_date": row.jackpot_date,
        "jackpot": _dollars(row.jackpot_text, row.jackpot_amount),
        "cash_value": _dollars(row.cash_value_text, row.cash_value_amount),
        "source": row.source,
        "seen_at": _stamp(row.last_seen_at),
    }


def _from_old_scraper(session, old: DrawResultRow, position: int, title: str | None) -> dict:
    """Powerball/Mega Millions before the switch: the old scraper's reading,
    plus PA's extras only if PA's reading of that same draw matched it."""
    entry = _entry(old, position, title)
    pa = session.scalar(select(DrawResultRow).filter_by(
        source=PA_FEED, game_id=old.game_id, draw_date=old.draw_date))
    check = session.scalar(select(DrawCheckRow).filter_by(
        game_id=old.game_id, draw_date=old.draw_date))
    if pa is not None and check is not None and check.matched:
        have = {e.get("code") for e in entry["extras"]}
        entry["extras"] += [e for e in json.loads(pa.extras or "[]") if e.get("code") not in have]
        entry["cash_value"] = _dollars(pa.cash_value_text, pa.cash_value_amount)
    return entry


def build_payload(session, now: datetime | None = None) -> dict:
    """The TV feed (see docs/pa-feeds-spec.md, "TV feed")."""
    source = pbmm_source(session)
    pa = _latest(session, PA_FEED)
    old = _latest(session, OLD_SCRAPER)
    if source != OLD_SCRAPER:
        # Switched: the old scraper only fills in a draw PA's feed doesn't have yet.
        old = {g: r for g, r in old.items()
               if g not in pa or r.draw_date > pa[g].draw_date}
    order = feed_order(session)
    # Before PA's feed has ever been read, the old scraper's games still show.
    order += [g for g in PBMM if g in old and g not in order]

    games = []
    for position, game_id in enumerate(order):
        title = pa[game_id].title if game_id in pa else None
        if game_id in PBMM and game_id in old:
            games.append(_from_old_scraper(session, old[game_id], position, title))
        elif game_id in pa:
            games.append(_entry(pa[game_id], position))
    return {
        "generated_at": _stamp(now or utcnow()),
        "pbmm_source": source,
        "games": games,
    }
