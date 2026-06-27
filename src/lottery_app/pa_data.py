"""Bridge between the shared PA catalog (scraper output) and the web app.

The scraper writes ``data/state.json`` twice a day — the full Pennsylvania
scratch-off catalog with prize tiers, odds, and metrics. Every store reads the
SAME file (one source of truth for the lottery data); only the *inventory* differs
per store. This module loads that catalog and turns it into the rows a store's
dashboard needs, reusing the exact KEEP / SEND-BACK logic and metrics from the
``lottery_tracker`` package so the web app and the static report never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lottery_tracker.model import Game
from lottery_tracker.rules import Thresholds, recommendation
from lottery_tracker.state import load_state


@dataclass
class Catalog:
    """The shared PA catalog plus when it was captured."""

    games: dict[str, Game]
    captured_at: str | None = None

    @property
    def active_count(self) -> int:
        return sum(1 for g in self.games.values() if g.status == "active")


def load_catalog(state_path: str | Path) -> Catalog:
    """Load the scraper's latest snapshot. Returns an empty catalog if missing."""
    p = Path(state_path)
    games = load_state(p)
    captured_at = None
    if p.exists():
        import json

        raw = json.loads(p.read_text() or "{}")
        captured_at = raw.get("captured_at")
    return Catalog(games=games, captured_at=captured_at)


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{min(1.0, x):.0%}"


def _row(g: Game, th: Thresholds) -> dict:
    """Flatten one game into the fields the dashboard template renders."""
    action, reason = recommendation(g, th)
    return {
        "game_number": g.game_number,
        "name": g.name,
        "price": g.price,
        "status": g.status,
        "action": action,                       # "keep" | "send_back"
        "reason": reason,
        "odds": g.odds,
        "odds_value": g.odds_value,
        "sell_through": g.sell_through_pct,
        "sell_through_str": _pct(g.sell_through_pct),
        "jackpot_density": g.jackpot_density,
        "top_prize_value": g.top_prize_value,
        "top_prizes_remaining": g.top_prizes_remaining,
        "top_prize_pct": g.top_prize_pct_remaining,
        "top_prize_pct_str": _pct(g.top_prize_pct_remaining),
        "tiers": g.tier_health(),
        "last_changed": g.last_changed,
        "sales_end_date": g.sales_end_date,
    }


def store_rows(catalog: Catalog, inventory: set[str], th: Thresholds) -> list[dict]:
    """Dashboard rows for the games a store carries.

    Includes inventory games even if they've vanished from the catalog (so the
    store still sees "this ended / was removed"). Sorted SEND BACK first (the
    things needing action), then by price high→low.
    """
    rows = []
    for num in inventory:
        g = catalog.games.get(num)
        if g is None:
            # In inventory but not in the PA catalog at all — most likely ended.
            rows.append({
                "game_number": num, "name": "(not on PA active list)", "price": None,
                "status": "unknown", "action": "send_back",
                "reason": "not found in PA catalog — likely ended, verify and pull",
                "odds": None, "odds_value": None, "sell_through": None,
                "sell_through_str": "—", "jackpot_density": None, "top_prize_value": None,
                "top_prizes_remaining": None, "top_prize_pct": None, "top_prize_pct_str": "—",
                "tiers": [], "last_changed": None, "sales_end_date": None,
            })
        else:
            rows.append(_row(g, th))
    rows.sort(key=lambda r: (r["action"] != "send_back", -(r["price"] or 0), r["game_number"]))
    return rows


def store_summary(rows: list[dict]) -> dict:
    keep = sum(1 for r in rows if r["action"] == "keep")
    send = sum(1 for r in rows if r["action"] == "send_back")
    return {"total": len(rows), "keep": keep, "send_back": send}


def bring_in_candidates(
    catalog: Catalog, inventory: set[str], th: Thresholds,
    *, min_left: float = 0.6, per_price: int = 4,
) -> dict[float, list[dict]]:
    """Best fresh games to BRING IN, grouped by price point.

    Catalog-wide: any active game NOT already carried, with strong odds and a high
    share of prizes still in the pack. Ranked by odds (best first) within each
    price. Returns {price: [rows]} for the dashboard's "bring in" board.
    """
    by_price: dict[float, list[dict]] = {}
    for g in catalog.games.values():
        if g.status != "active" or g.game_number in inventory or g.price is None:
            continue
        left = g.sell_through_pct
        if left is not None and left < min_left:
            continue
        if th.weak_odds is not None and g.odds_value is not None and g.odds_value > th.weak_odds:
            continue
        by_price.setdefault(g.price, []).append(_row(g, th))
    for price, rows in by_price.items():
        rows.sort(key=lambda r: (r["odds_value"] is None, r["odds_value"] or 9e9))
        by_price[price] = rows[:per_price]
    return dict(sorted(by_price.items(), key=lambda kv: -kv[0]))


def new_games(catalog: Catalog, within_days: int = 14) -> list[dict]:
    """Recently-appeared active games (excludes the baseline first-seen cohort)."""
    from lottery_tracker.notify import new_games as _ng

    # Use the captured_at as "now" so the window is relative to the latest scrape.
    now = catalog.captured_at or ""
    if not now:
        return []
    th = Thresholds()
    return [_row(g, th) for g in _ng(catalog.games, now, within_days=within_days)]
