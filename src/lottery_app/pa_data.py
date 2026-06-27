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
from lottery_tracker.rules import RatingWeights, Thresholds, rate, recommendation
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


def _row(g: Game, th: Thresholds, weights: RatingWeights) -> dict:
    """Flatten one game into the fields the dashboard template renders."""
    action, reason = recommendation(g, th, weights)
    rating, _factors = rate(g, weights)
    pct_all = g.overall_pct_remaining
    if pct_all is None:
        pct_all = g.top_prize_pct_remaining
    return {
        "game_number": g.game_number,
        "name": g.name,
        "price": g.price,
        "status": g.status,
        "action": action,                       # "keep" | "send_back"
        "reason": reason,
        "rating": rating,
        "rating_str": "—" if rating is None else f"{rating:.0f}",
        "odds": g.odds,
        "odds_value": g.odds_value,
        "pct_all": pct_all,
        "pct_all_str": _pct(pct_all),
        "low_prize_pct": g.low_prize_pct_remaining,
        "low_prize_str": _pct(g.low_prize_pct_remaining),
        "jackpot_density": g.jackpot_density,
        "jackpot_significant": g.jackpot_density_significant,
        "top_prize_value": g.top_prize_value,
        "top_prizes_remaining": g.top_prizes_remaining,
        "tiers": g.tier_health(),
        "last_changed": g.last_changed,
        "sales_end_date": g.sales_end_date,
    }


def store_rows(catalog: Catalog, inventory: set[str], th: Thresholds,
               weights: RatingWeights | None = None) -> list[dict]:
    """Dashboard rows for the games a store carries.

    Includes inventory games even if they've vanished from the catalog (so the
    store still sees "this ended / was removed"). Sorted SEND BACK first (the
    things needing action), then by worst rating.
    """
    weights = weights or RatingWeights()
    rows = []
    for num in inventory:
        g = catalog.games.get(num)
        if g is None:
            # In inventory but not in the PA catalog at all — most likely ended.
            rows.append({
                "game_number": num, "name": "(not on PA active list)", "price": None,
                "status": "unknown", "action": "send_back",
                "reason": "not found in PA catalog — likely ended, verify and pull",
                "rating": None, "rating_str": "—",
                "odds": None, "odds_value": None, "pct_all": None, "pct_all_str": "—",
                "low_prize_pct": None, "low_prize_str": "—",
                "jackpot_density": None, "jackpot_significant": False, "top_prize_value": None,
                "top_prizes_remaining": None, "tiers": [], "last_changed": None,
                "sales_end_date": None,
            })
        else:
            rows.append(_row(g, th, weights))
    # Pair every SEND BACK with the best same-price replacements you don't carry.
    for r in rows:
        r["swap_to"] = (swap_targets(catalog, inventory, r.get("price"), th, weights, n=3)
                        if r["action"] == "send_back" else [])
    rows.sort(key=lambda r: (r["action"] != "send_back",
                             r["rating"] if r["rating"] is not None else 999))
    return rows


def catalog_rankings(
    catalog: Catalog, th: Thresholds, weights: RatingWeights | None = None,
    *, inventory: set[str] | None = None,
) -> list[dict]:
    """Every active game, rated and ranked best-first. Marks which ones the store
    already carries so the UI can show "carried" vs "available to bring in"."""
    weights = weights or RatingWeights()
    inv = inventory or set()
    rows = []
    for g in catalog.games.values():
        if g.status != "active":
            continue
        row = _row(g, th, weights)
        row["carried"] = g.game_number in inv
        rows.append(row)
    rows.sort(key=lambda r: (r["rating"] if r["rating"] is not None else -1), reverse=True)
    return rows


def swap_targets(
    catalog: Catalog, inventory: set[str], price: float | None, th: Thresholds,
    weights: RatingWeights | None = None, *, n: int = 3,
) -> list[dict]:
    """Best replacement games at the SAME price as something you're sending back:
    active, not already carried, and KEEP-worthy (rating above the cutoff), highest
    first. Freshness is already baked into the rating, so we don't double-gate it —
    if nothing clears the cutoff, there simply isn't a good same-price swap (a useful
    signal that the price point itself may be picked over)."""
    weights = weights or RatingWeights()
    if price is None:
        return []
    cands = []
    for g in catalog.games.values():
        if g.status != "active" or g.game_number in inventory or g.price != price:
            continue
        row = _row(g, th, weights)
        if row["rating"] is None or row["rating"] < weights.cutoff:
            continue
        cands.append(row)
    cands.sort(key=lambda r: r["rating"], reverse=True)
    return cands[:n]


def store_summary(rows: list[dict]) -> dict:
    keep = sum(1 for r in rows if r["action"] == "keep")
    send = sum(1 for r in rows if r["action"] == "send_back")
    return {"total": len(rows), "keep": keep, "send_back": send}


def bring_in_candidates(
    catalog: Catalog, inventory: set[str], th: Thresholds,
    *, weights: RatingWeights | None = None, min_left: float = 0.6, per_price: int = 4,
) -> dict[float, list[dict]]:
    """Best fresh games to BRING IN, grouped by price point.

    Catalog-wide: any active game NOT already carried, with strong odds and a high
    share of prizes still in the pack (robust % remaining). Ranked by odds (best
    first) within each price. Returns {price: [rows]} for the "bring in" board.
    """
    weights = weights or RatingWeights()
    by_price: dict[float, list[dict]] = {}
    for g in catalog.games.values():
        if g.status != "active" or g.game_number in inventory or g.price is None:
            continue
        left = g.overall_pct_remaining
        if left is None or left < min_left:
            continue
        if th.weak_odds is not None and g.odds_value is not None and g.odds_value > th.weak_odds:
            continue
        by_price.setdefault(g.price, []).append(_row(g, th, weights))
    for price, rows in by_price.items():
        rows.sort(key=lambda r: (r["odds_value"] is None, r["odds_value"] or 9e9))
        by_price[price] = rows[:per_price]
    return dict(sorted(by_price.items(), key=lambda kv: -kv[0]))


def new_games(catalog: Catalog, within_days: int = 14,
              weights: RatingWeights | None = None) -> list[dict]:
    """Recently-appeared active games (excludes the baseline first-seen cohort)."""
    from lottery_tracker.notify import new_games as _ng

    # Use the captured_at as "now" so the window is relative to the latest scrape.
    now = catalog.captured_at or ""
    if not now:
        return []
    th = Thresholds()
    weights = weights or RatingWeights()
    return [_row(g, th, weights) for g in _ng(catalog.games, now, within_days=within_days)]
