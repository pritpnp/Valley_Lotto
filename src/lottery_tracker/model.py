"""Canonical data model for a scratch-off game.

A single ``Game`` row is the normalized merge of what we can learn from both
PA Lottery pages: the "Prizes Remaining" page (active games + prize counts) and
the "Sales Ended" page (games whose sales have stopped + claim deadlines).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Game:
    game_number: str                       # PA's game number, e.g. "5432" — our primary key
    name: str = ""
    price: Optional[float] = None          # ticket price in dollars

    # Status comes from which page the game appears on.
    status: str = "active"                 # "active" | "ended"

    # --- Date fields (from the Active / Sales-Ended pages) ---
    on_sale_date: Optional[str] = None     # when the game started selling (e.g. "06/2026")
    sales_end_date: Optional[str] = None   # last day the game is sold (set once ended)
    claim_deadline: Optional[str] = None   # last day a winning ticket can be redeemed

    # --- Prizes-Remaining page fields ---
    # PA's "Remaining" page lists the top six prize tiers (values) and how many of
    # each are still unclaimed ("wins remaining"). tier 1 = the top prize.
    top_prize_value: Optional[str] = None  # e.g. "$100,000" — first of the six tiers
    top_prizes_remaining: Optional[int] = None  # wins remaining for the top tier
    prize_tiers: list = field(default_factory=list)  # [{"value": "$17,000", "remaining": 6}, ...]

    # Original top-prize count. Preferred source is the game's detail page
    # ("offers N Top Prizes of $X"); when that's unavailable we fall back to the
    # highest "wins remaining" ever seen (flagged via total_is_estimate).
    top_prizes_total: Optional[int] = None
    total_is_estimate: bool = True          # False once we have the true count from the detail page

    # From the detail page.
    detail_id: Optional[str] = None         # PA internal id used by View-Scratch-Off.aspx?id=
    odds: Optional[str] = None              # overall odds, e.g. "1:3.38"

    # Bookkeeping
    source_pages: list = field(default_factory=list)  # which pages contributed to this row

    # ---- derived helpers -------------------------------------------------
    @property
    def top_prize_pct_remaining(self) -> Optional[float]:
        """Fraction (0..1) of the top prizes that are still unclaimed, or None if unknown."""
        if self.top_prizes_total and self.top_prizes_total > 0 and self.top_prizes_remaining is not None:
            return self.top_prizes_remaining / self.top_prizes_total
        return None

    @property
    def lower_wins_remaining(self) -> Optional[int]:
        """Count of NON-jackpot wins still out there (published tiers below the top).

        This is the low-prize availability the retailer cares about: how many of the
        common, cheaper-to-win prizes are still in the pack. (PA only publishes the
        top six tiers, so the very smallest break-even prizes aren't included — the
        overall win odds cover those.)
        """
        if not self.prize_tiers or len(self.prize_tiers) < 2:
            return None
        vals = [t.get("remaining") for t in self.prize_tiers[1:] if t.get("remaining") is not None]
        return sum(vals) if vals else None

    @property
    def odds_value(self) -> Optional[float]:
        """Overall odds as a number (the X in '1:X'); lower = better chance to win.

        This is the published chance that a ticket wins ANY prize — effectively the
        chance a player at least breaks even, and it stays ~constant over a game's
        life (the small, common prizes dominate it and deplete in step with sales).
        """
        if not self.odds:
            return None
        try:
            return float(self.odds.split(":")[-1])
        except (ValueError, IndexError):
            return None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Game":
        # Tolerate snapshots written by older versions: ignore unknown keys.
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def merge_games(
    remaining: list[Game],
    ended: list[Game],
    active: list[Game] | None = None,
) -> dict[str, Game]:
    """Merge the page parses into one dict keyed by game_number.

    Sources:
      * ``remaining``  — prize counts for active games.
      * ``active``     — authoritative list of games still on sale (ActivePrint).
      * ``ended``      — games whose sales have stopped, with claim deadlines.

    Precedence: a game on the ``ended`` list is marked ended regardless of the
    other pages (PA can keep an ended game on the remaining list for a while so
    winners can still redeem). The ``active`` set is recorded so callers can tell
    "still selling" from "ended but prizes still claimable".
    """
    by_num: dict[str, Game] = {}

    def _upsert(g: Game) -> Game:
        cur = by_num.get(g.game_number)
        if cur is None:
            by_num[g.game_number] = g
            return g
        # Merge field-by-field, preferring already-populated values.
        cur.name = cur.name or g.name
        cur.price = cur.price if cur.price is not None else g.price
        cur.on_sale_date = cur.on_sale_date or g.on_sale_date
        cur.sales_end_date = cur.sales_end_date or g.sales_end_date
        cur.claim_deadline = cur.claim_deadline or g.claim_deadline
        cur.top_prize_value = cur.top_prize_value or g.top_prize_value
        cur.prize_tiers = cur.prize_tiers or g.prize_tiers
        for f in ("top_prizes_total", "top_prizes_remaining"):
            if getattr(cur, f) is None and getattr(g, f) is not None:
                setattr(cur, f, getattr(g, f))
        for p in g.source_pages:
            if p not in cur.source_pages:
                cur.source_pages.append(p)
        return cur

    for g in remaining:
        _upsert(g)
    for g in (active or []):
        _upsert(g)
    for g in ended:
        merged = _upsert(g)
        merged.status = "ended"  # ended always wins
        if "sales_ended" not in merged.source_pages:
            merged.source_pages.append("sales_ended")

    return by_num


def estimate_top_prize_totals(
    current: dict[str, "Game"], previous: dict[str, "Game"]
) -> None:
    """Estimate each game's original top-prize count as the highest 'wins
    remaining' ever seen (this run or any prior snapshot).

    PA never publishes the original print count, so this running maximum is our
    best proxy: exact for games first seen as NEW, a lower bound for older games
    (which only makes the % *less* alarming, never a false alarm). Mutates
    ``current`` in place, setting ``top_prizes_total``.
    """
    for num, g in current.items():
        if g.top_prizes_remaining is None:
            continue
        if g.top_prizes_total is not None and not g.total_is_estimate:
            continue  # we already have the true original count from the detail page
        prev = previous.get(num)
        seen_max = g.top_prizes_remaining
        if prev is not None and (prev.total_is_estimate is not False):
            seen_max = max(seen_max, prev.top_prizes_total or 0, prev.top_prizes_remaining or 0)
        g.top_prizes_total = seen_max
        g.total_is_estimate = True
