"""Canonical data model for a scratch-off game.

A single ``Game`` row is the normalized merge of what we can learn from both
PA Lottery pages: the "Prizes Remaining" page (active games + prize counts) and
the "Sales Ended" page (games whose sales have stopped + claim deadlines).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Optional


def _money_to_num(s: Optional[str]) -> Optional[float]:
    """'$5,000' -> 5000.0, tolerant of stray text; None if no number found."""
    if not s:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", s)
    return float(m.group(0).replace(",", "")) if m else None


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

    # From the PA Bulletin (official full prize structure).
    tickets_printed: Optional[int] = None
    payout_pct: Optional[float] = None
    tier_originals: dict = field(default_factory=dict)  # {value_num_str: original_count}

    # Change tracking (filled in by update_change_tracking each run).
    first_seen: Optional[str] = None      # when we first recorded this game
    last_changed: Optional[str] = None    # last time we OBSERVED its data move (None = not yet)

    # Bookkeeping
    source_pages: list = field(default_factory=list)  # which pages contributed to this row

    # ---- derived helpers -------------------------------------------------
    @property
    def top_prize_pct_remaining(self) -> Optional[float]:
        """Fraction (0..1) of the top prizes that are still unclaimed, or None if unknown."""
        if self.top_prizes_total and self.top_prizes_total > 0 and self.top_prizes_remaining is not None:
            return self.top_prizes_remaining / self.top_prizes_total
        return None

    def tier_table(self, *, prev_tiers: list | None = None) -> list[dict]:
        """Return the prize tiers (high→low value) with bottom-to-top weights and
        the change in wins-remaining since the previous snapshot.

        Each row: {value, value_num, remaining, delta, weight}. Weight increases
        toward the bottom (cheapest prize = heaviest), per the retailer's rule that
        the low/break-even prizes matter most.
        """
        tiers = self.prize_tiers or []
        n = len(tiers)
        prev_by_value = {}
        for t in (prev_tiers or []):
            prev_by_value[t.get("value")] = t.get("remaining")
        rows = []
        for i, t in enumerate(tiers):
            rem = t.get("remaining")
            prev = prev_by_value.get(t.get("value"))
            delta = (rem - prev) if (rem is not None and prev is not None) else None
            rows.append({
                "value": t.get("value"),
                "value_num": _money_to_num(t.get("value")),
                "remaining": rem,
                "delta": delta,
                "weight": i + 1,  # i=0 is the top (highest) prize → lightest; bottom → heaviest
            })
        return rows

    def tier_health(self) -> list[dict]:
        """Per published tier: value, remaining, true original (from the Bulletin),
        % remaining, and bottom-to-top weight. ``pct`` is None when we lack the
        original count for that tier.
        """
        rows = []
        tiers = self.prize_tiers or []
        for i, t in enumerate(tiers):
            v = _money_to_num(t.get("value"))
            rem = t.get("remaining")
            orig = self.tier_originals.get(str(v)) if (v is not None and self.tier_originals) else None
            pct = (rem / orig) if (orig and rem is not None and orig > 0) else None
            rows.append({"value": t.get("value"), "value_num": v, "remaining": rem,
                         "original": orig, "pct": pct, "weight": i + 1})
        return rows

    @property
    def sell_through_pct(self) -> Optional[float]:
        """Best gauge of how much of the WHOLE game is unsold: the % remaining of the
        lowest-value tracked tier (it has the most prizes, so it's statistically the
        most precise — and under PA's even distribution every tier tracks it)."""
        rows = [r for r in self.tier_health() if r["pct"] is not None and r["value_num"] is not None]
        return min(rows, key=lambda r: r["value_num"])["pct"] if rows else None

    @property
    def jackpot_density(self) -> Optional[float]:
        """Top-tier % remaining ÷ sell-through %. >1 = top prizes are hanging around
        longer than the game's sell-through predicts (good for jackpot hunters);
        <1 = the big prizes were claimed early (picked over)."""
        rows = [r for r in self.tier_health() if r["pct"] is not None and r["value_num"] is not None]
        anchor = self.sell_through_pct
        if len(rows) < 2 or not anchor:
            return None
        top = max(rows, key=lambda r: r["value_num"])["pct"]
        return top / anchor

    @property
    def weighted_low_health(self) -> Optional[float]:
        """Weighted-average % remaining across tracked tiers, with the CHEAPEST tiers
        weighted heaviest (the retailer's rule). With true originals this is a real
        number; in practice it sits near the sell-through % because tiers deplete
        together, and dips when the low tiers run down faster than the top."""
        rows = [r for r in self.tier_health() if r["pct"] is not None and r["value_num"] is not None]
        if not rows:
            return None
        rows.sort(key=lambda r: r["value_num"])  # ascending: cheapest first
        n = len(rows)
        acc = wsum = 0.0
        for rank, r in enumerate(rows):
            w = n - rank  # cheapest gets the highest weight
            acc += w * r["pct"]
            wsum += w
        return acc / wsum if wsum else None

    # ---- robust, outlier-aware metrics ----------------------------------
    def _tiers_with_orig(self) -> list[dict]:
        """tier_health rows that have a real original count we can do stats on."""
        return [
            r for r in self.tier_health()
            if r["original"] and r["original"] > 0
            and r["remaining"] is not None and r["value_num"] is not None
        ]

    @property
    def overall_pct_remaining(self) -> Optional[float]:
        """True fraction of the game left = Σ(wins remaining) / Σ(original wins)
        across every tracked tier.

        This is the statistically sound "% of the game unsold". It is mathematically
        an inverse-variance-weighted average of the per-tier fractions, so it is
        dominated by the abundant low tiers (thousands of prizes → tiny error) and
        barely moved by the noisy top tier (a handful of prizes). It does NOT rely on
        the jackpot count the way the old top-prize-only estimate did.
        """
        rows = self._tiers_with_orig()
        if not rows:
            return None
        rem = sum(r["remaining"] for r in rows)
        orig = sum(r["original"] for r in rows)
        return rem / orig if orig > 0 else None

    @property
    def low_prize_pct_remaining(self) -> Optional[float]:
        """% of the CHEAP prizes still in the pack (the cheapest half of the tracked
        tiers, by value) = Σremaining / Σoriginal over those tiers.

        These are the prizes a normal customer actually wins, so this is the
        "incentive to play" signal — if the cheap prizes are gone, players have no
        reason to buy even when a jackpot is technically still out there.
        """
        rows = sorted(self._tiers_with_orig(), key=lambda r: r["value_num"])
        if not rows:
            return None
        k = max(1, len(rows) // 2)  # cheapest half (at least one tier)
        low = rows[:k]
        rem = sum(r["remaining"] for r in low)
        orig = sum(r["original"] for r in low)
        return rem / orig if orig > 0 else None

    def tier_z_scores(self) -> list[dict]:
        """How far each tier deviates from the REST of the game, in standard
        deviations (z-score). This is the outlier check the data needs.

        Under PA's uniform shuffle every tier should deplete at the same rate. We
        compare each tier against the pooled rate of all the *other* tiers (a
        leave-one-out reference ``p`` — important, because the abundant cheap tier
        otherwise defines the average and could never look like an outlier against
        itself). For a tier with N original prizes the count still out is
        ~Binomial(N, p): expected = p·N, standard deviation = √(N·p·(1−p)),
        z = (remaining − expected) / sd.

        A tier is flagged *significant* when |z| clears a **Bonferroni-corrected**
        threshold: because we test every tier at once, a flat |z|≥2 cutoff would raise
        a false "signal" ~26% of the time on a 6-tier game. We instead require the
        family-wise error rate to stay at ``alpha`` (5%) across all k tiers, i.e.
        |z| ≥ Φ⁻¹(1 − alpha/2k) — about 2.6σ for six tiers. Everything below that is
        treated as small-sample noise (which is why a 2-of-3 top prize means nothing).
        Returns one row per tracked tier: {value, value_num, z, crit, significant,
        remaining, original}.
        """
        from statistics import NormalDist

        rows = self._tiers_with_orig()
        if len(rows) < 2:
            return []
        k = len(rows)
        alpha = 0.05
        crit = NormalDist().inv_cdf(1 - alpha / (2 * k))  # family-wise 5% over k tiers
        tot_rem = sum(r["remaining"] for r in rows)
        tot_orig = sum(r["original"] for r in rows)
        out = []
        for r in rows:
            n, rem = r["original"], r["remaining"]
            # Pool the OTHER tiers as the reference rate (leave-one-out).
            other_orig = tot_orig - n
            p = (tot_rem - rem) / other_orig if other_orig > 0 else None
            if p is None or not (0 < p < 1):
                z = 0.0
            else:
                sd = (n * p * (1 - p)) ** 0.5
                z = (rem - p * n) / sd if sd > 0 else 0.0
            out.append({
                "value": r["value"], "value_num": r["value_num"], "z": z, "crit": crit,
                "significant": abs(z) >= crit, "remaining": rem, "original": n,
            })
        return out

    @property
    def jackpot_density_significant(self) -> bool:
        """True only when the top tier's deviation from sell-through is statistically
        real (|z| ≥ 2). When False, the jackpot-density number is just small-sample
        noise and should not drive any decision."""
        zs = self.tier_z_scores()
        if not zs:
            return False
        top = max(zs, key=lambda r: r["value_num"])
        return top["significant"]

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
        if "sales_ended" not in merged.source_pages:
            merged.source_pages.append("sales_ended")

    # RULE #1: the ActivePrint list is the source of truth for "still selling".
    # If a game isn't on it, it's dead — regardless of the other pages.
    active_set = {g.game_number for g in (active or [])}
    if active_set:  # only enforce when we actually have the active list
        for num, g in by_num.items():
            g.status = "active" if num in active_set else "ended"

    return by_num


def _game_fingerprint(g: "Game") -> str:
    """Per-game fingerprint of the data we treat as 'a change' (status + tiers)."""
    return json.dumps(
        {
            "status": g.status,
            "on_sale_date": g.on_sale_date,
            "sales_end_date": g.sales_end_date,
            "tiers": [[t.get("value"), t.get("remaining")] for t in (g.prize_tiers or [])],
        },
        sort_keys=True, separators=(",", ":"),
    )


def update_change_tracking(
    current: dict[str, "Game"], previous: dict[str, "Game"], captured_at: str
) -> None:
    """Carry forward each game's first_seen / last_changed.

    last_changed is set only when we actually OBSERVE the data move; until then it
    stays None (we don't pretend a game "changed today" just because tracking
    started today). Mutates ``current`` in place.
    """
    for num, g in current.items():
        prev = previous.get(num)
        if prev is None:
            g.first_seen = captured_at
            g.last_changed = None
        else:
            g.first_seen = prev.first_seen or captured_at
            g.last_changed = (
                prev.last_changed if _game_fingerprint(prev) == _game_fingerprint(g)
                else captured_at
            )


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
