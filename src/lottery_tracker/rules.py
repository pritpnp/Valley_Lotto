"""Turn two snapshots (previous vs. current) into actionable alerts.

Two things the retailer asked to be told about:

  1. A game ENDED — sales stopped. Highest priority when it's a game they carry,
     because unsold inventory of an ended game is dead stock and winners have a
     claim deadline.
  2. A game's PRIZES ARE TOO LOW to be worth keeping — time to swap it for a
     fresh game. "Too low" is configurable (see ``Thresholds``).

Alerts fire on *transitions* (it just became true), so you aren't re-pinged daily
about the same game. A game you carry that simply vanishes from all PA pages is
treated as "ended/removed" too.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from .model import Game

# The five factors a store can emphasize (slider order on the UI).
RATING_FACTORS = ("odds", "prizes_left", "low_prize", "low_prize_skew", "jackpot_density")


class Severity(str, Enum):
    INFO = "info"
    WARN = "warning"
    CRITICAL = "critical"


@dataclass
class Thresholds:
    top_prize_pct: float | None = 0.40      # SWAP when top prizes remaining < this fraction of the original
    top_prize_count_floor: int | None = 1   # OR when top prizes remaining <= this absolute count (and depleting)
    weak_odds: float | None = 4.5           # mark games whose overall odds are worse than 1:this

    @classmethod
    def from_config(cls, cfg: dict | None) -> "Thresholds":
        cfg = cfg or {}
        return cls(
            top_prize_pct=cfg.get("top_prize_pct", 0.40),
            top_prize_count_floor=cfg.get("top_prize_count_floor", 1),
            weak_odds=cfg.get("weak_odds", 4.5),
        )


@dataclass
class RatingWeights:
    """The factors that decide KEEP vs SEND BACK, and how much each one counts.

    Each factor is scored 0–100 (100 = great, 0 = terrible). The final rating is the
    weighted average of the factors we have data for, and a game is SENT BACK when
    its rating falls below ``cutoff``. Edit the weights in config.yaml to say which
    factors matter most to you — set a weight to 0 to ignore that factor entirely.

    Factors:
      odds            — overall chance to win ANY prize (1:X). The break-even signal.
      prizes_left     — true % of the whole game still unsold (robust Σrem/Σorig).
      low_prize       — % of the CHEAP prizes left (what customers actually win).
      low_prize_skew  — are the cheap prizes drying up FASTER than the game overall?
                        (only counts when statistically significant, |z| ≥ 2.)
      jackpot_density — top prizes vs sell-through. Off by default-ish (low weight)
                        because it's not what players aim for, and it only counts at
                        all when it's a real signal, not small-sample noise.
    """

    odds: float = 30.0
    prizes_left: float = 25.0
    low_prize: float = 25.0
    low_prize_skew: float = 15.0
    jackpot_density: float = 5.0

    # Tuning knobs (you rarely need to touch these).
    odds_good: float = 3.0       # 1:3.0 or better → full marks on odds
    odds_bad: float = 5.0        # 1:5.0 or worse  → zero on odds
    skew_z_full: float = 6.0     # a −6σ cheap-prize outlier → zero on the skew factor
    cutoff: float = 50.0         # rating below this → SEND BACK

    @classmethod
    def from_config(cls, cfg: dict | None) -> "RatingWeights":
        cfg = cfg or {}
        d = {}
        for f in cls.__dataclass_fields__:  # type: ignore[attr-defined]
            if f in cfg and cfg[f] is not None:
                d[f] = float(cfg[f])
        return cls(**d)

    def scaled(self, emphasis: dict[str, float] | None, *, step: float = 1.6) -> "RatingWeights":
        """Apply per-store *emphasis* sliders to the base weights.

        Each slider sits at 0 in the middle (use the base weight as-is). Pushing it
        up/down by one notch multiplies/divides that factor's weight by ``step``
        (~1.6×), so +2 ≈ 2.6× the emphasis and −2 ≈ 0.4×. Only the relative sizes
        matter (the rating renormalizes by total weight), so this is exactly a
        "more emphasis here / less there" control. Tuning knobs are unchanged.
        """
        emphasis = emphasis or {}
        new = {f: getattr(self, f) * (step ** float(emphasis.get(f, 0.0))) for f in RATING_FACTORS}
        return replace(self, **new)


@dataclass
class Factor:
    """One scored input to a game's rating (for display + the decision)."""
    key: str
    label: str
    score: float | None     # 0..100, or None when we have no data for it
    weight: float
    detail: str             # short human explanation of the value


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def rate(game: Game, weights: "RatingWeights | None" = None) -> tuple[float | None, list[Factor]]:
    """Score a game 0–100 from several weighted, outlier-aware factors.

    Returns (rating, factors). ``rating`` is None only when we have no usable data at
    all. Factors with ``score is None`` are excluded from the weighted average (and
    the remaining weights are renormalized), so a missing input never silently drags
    a game down — it just doesn't vote.
    """
    w = weights or RatingWeights()
    factors: list[Factor] = []

    # 1) Win odds — the chance to win anything.
    if game.odds_value is not None:
        s = _clamp(100 * (w.odds_bad - game.odds_value) / (w.odds_bad - w.odds_good))
        factors.append(Factor("odds", "Win odds", s, w.odds, f"1:{game.odds_value:g}"))
    else:
        factors.append(Factor("odds", "Win odds", None, w.odds, "—"))

    # 2) Prizes left — true % of the game unsold (robust; falls back to top-prize %
    #    only when we have no per-tier originals yet).
    pl = game.overall_pct_remaining
    if pl is None:
        pl = game.top_prize_pct_remaining
    if pl is not None:
        factors.append(Factor("prizes_left", "Prizes left", _clamp(100 * min(1.0, pl)),
                              w.prizes_left, f"{min(1.0, pl):.0%} of all prizes left"))
    else:
        factors.append(Factor("prizes_left", "Prizes left", None, w.prizes_left, "—"))

    # 3) Low-prize stock — % of the cheap, winnable prizes left.
    lp = game.low_prize_pct_remaining
    if lp is not None:
        factors.append(Factor("low_prize", "Low-prize stock", _clamp(100 * min(1.0, lp)),
                              w.low_prize, f"{min(1.0, lp):.0%} of cheap prizes left"))
    else:
        factors.append(Factor("low_prize", "Low-prize stock", None, w.low_prize, "—"))

    # 4) Low-prize skew — are the cheap prizes draining FASTER than the game overall?
    #    Only a statistically significant negative z-score counts (everything else is
    #    small-sample noise).
    zs = game.tier_z_scores()
    if zs:
        zs_sorted = sorted(zs, key=lambda r: r["value_num"])
        low = zs_sorted[: max(1, len(zs_sorted) // 2)]
        sig_neg = [r["z"] for r in low if r["significant"] and r["z"] < 0]
        if sig_neg:
            worst = min(sig_neg)
            s = _clamp(100 * (1 - (-worst) / w.skew_z_full))
            factors.append(Factor("low_prize_skew", "Low-prize trend", s, w.low_prize_skew,
                                  f"cheap prizes {worst:+.1f}σ vs game — drying up"))
        else:
            factors.append(Factor("low_prize_skew", "Low-prize trend", 100.0, w.low_prize_skew,
                                  "in line with the game"))
    else:
        factors.append(Factor("low_prize_skew", "Low-prize trend", None, w.low_prize_skew, "—"))

    # 5) Jackpot density — only votes when it's a real signal, not noise.
    jd = game.jackpot_density
    if jd is not None and game.jackpot_density_significant:
        factors.append(Factor("jackpot_density", "Jackpot density", _clamp(100 * min(1.0, jd)),
                              w.jackpot_density, f"{jd:.2f}× (significant)"))
    else:
        note = f"{jd:.2f}× (noise — ignored)" if jd is not None else "—"
        factors.append(Factor("jackpot_density", "Jackpot density", None, w.jackpot_density, note))

    avail = [f for f in factors if f.score is not None and f.weight > 0]
    tot_w = sum(f.weight for f in avail)
    rating = sum(f.weight * f.score for f in avail) / tot_w if tot_w > 0 else None
    return rating, factors


@dataclass
class Alert:
    kind: str                      # "ended" | "low_prizes" | "removed"
    game_number: str
    name: str
    severity: Severity
    message: str
    owned: bool = False
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["severity"] = self.severity.value
        return d


def recommendation(
    game: Game, th: Thresholds, weights: "RatingWeights | None" = None
) -> tuple[str, str]:
    """One clear call per game: ("keep" | "send_back", reason).

    Driven by the weighted 0–100 rating (see ``rate``): SEND BACK when sales have
    ended, or when the rating falls below the cutoff. The reason names the factors
    that hurt the score the most, so you can see *why* — and re-weight what matters
    to you in config.yaml.
    """
    if game.status == "ended":
        when = f" {game.sales_end_date}" if game.sales_end_date else ""
        return ("send_back", f"sales ended{when} — pull it")

    w = weights or RatingWeights()
    score, factors = rate(game, w)
    if score is None:
        return ("keep", "not enough data yet — keeping")

    # Biggest drags first: low score × high weight.
    drags = sorted(
        (f for f in factors if f.score is not None and f.weight > 0),
        key=lambda f: f.weight * (100 - f.score), reverse=True,
    )
    weak_bits = [f.detail for f in drags[:2] if f.score < 60]
    if score < w.cutoff:
        why = "; ".join(weak_bits) if weak_bits else "weak across the board"
        return ("send_back", f"rating {score:.0f}/100 — {why}")
    tail = f" — watch: {weak_bits[0]}" if weak_bits else " — healthy"
    return ("keep", f"rating {score:.0f}/100{tail}")


def _is_low(game: Game, th: Thresholds) -> tuple[bool, list[str]]:
    """Return (is_low, reasons). A game is low if ANY configured rule trips."""
    reasons: list[str] = []
    pct = game.top_prize_pct_remaining
    if th.top_prize_pct is not None and pct is not None and pct < th.top_prize_pct:
        reasons.append(
            f"only {pct:.0%} of top prizes left "
            f"({game.top_prizes_remaining}/{game.top_prizes_total})"
        )
    # Count floor: only meaningful once the game has actually started depleting.
    # A game that simply HAS one top prize and hasn't sold any sits at 100% — not
    # "low" — so we require remaining to be below the estimated original.
    depleting = game.top_prizes_total is None or (
        game.top_prizes_remaining is not None
        and game.top_prizes_remaining < game.top_prizes_total
    )
    if (
        th.top_prize_count_floor is not None
        and game.top_prizes_remaining is not None
        and game.top_prizes_remaining <= th.top_prize_count_floor
        and depleting
    ):
        reasons.append(f"{game.top_prizes_remaining} top prize(s) remaining")
    return (bool(reasons), reasons)


def evaluate(
    current: dict[str, Game],
    previous: dict[str, Game],
    *,
    inventory: set[str],
    thresholds: Thresholds,
    report_all_games: bool = False,
) -> list[Alert]:
    """Compare snapshots and return alerts for new transitions."""
    alerts: list[Alert] = []

    def owned(num: str) -> bool:
        return num in inventory

    # --- 1. Games that just ENDED -------------------------------------------
    for num, g in current.items():
        was = previous.get(num)
        just_ended = g.status == "ended" and (was is None or was.status != "ended")
        if just_ended:
            sev = Severity.CRITICAL if owned(num) else Severity.INFO
            who = "A game you carry" if owned(num) else "A game"
            when = f" (ended {g.sales_end_date})" if g.sales_end_date else ""
            started = f" Started {g.on_sale_date}." if g.on_sale_date else ""
            extra = ""
            if g.claim_deadline:
                extra = f" Last day to redeem winners: {g.claim_deadline}."
            alerts.append(
                Alert(
                    kind="ended",
                    game_number=num,
                    name=g.name,
                    severity=sev,
                    owned=owned(num),
                    message=f"{who} ENDED sales: #{num} {g.name}{when}.{started}{extra}",
                    details={
                        "on_sale_date": g.on_sale_date,
                        "sales_end_date": g.sales_end_date,
                        "claim_deadline": g.claim_deadline,
                        "price": g.price,
                    },
                )
            )

    # --- 2. Owned games that vanished from all pages (treat as removed) ------
    for num in inventory:
        if num not in current and num in previous and previous[num].status != "ended":
            g = previous[num]
            alerts.append(
                Alert(
                    kind="removed",
                    game_number=num,
                    name=g.name,
                    severity=Severity.WARN,
                    owned=True,
                    message=(
                        f"A game you carry dropped off the PA active list: "
                        f"#{num} {g.name}. It has most likely ended — verify and pull stock."
                    ),
                    details={"last_seen_status": g.status},
                )
            )

    # --- 3. Active games whose prizes just got TOO LOW ----------------------
    for num, g in current.items():
        if g.status != "active":
            continue
        if not report_all_games and not owned(num):
            continue
        low_now, reasons = _is_low(g, thresholds)
        if not low_now:
            continue
        was = previous.get(num)
        was_low = False
        if was is not None:
            was_low, _ = _is_low(was, thresholds)
        if was_low:
            continue  # already alerted on a prior run
        sev = Severity.WARN if owned(num) else Severity.INFO
        who = "A game you carry" if owned(num) else "A game"
        alerts.append(
            Alert(
                kind="low_prizes",
                game_number=num,
                name=g.name,
                severity=sev,
                owned=owned(num),
                message=(
                    f"{who} is running low — consider swapping it for a fresh game: "
                    f"#{num} {g.name} ({'; '.join(reasons)})."
                ),
                details={
                    "reasons": reasons,
                    "top_prizes_remaining": g.top_prizes_remaining,
                    "top_prizes_total": g.top_prizes_total,
                    "top_prize_value": g.top_prize_value,
                },
            )
        )

    # --- 4. Brand-new games that just appeared on the ActivePrint list ------
    for num, g in current.items():
        if g.status != "active" or num in previous:
            continue  # only games we've never seen before
        bits = []
        if g.price is not None:
            bits.append(f"${g.price:g}")
        if g.odds_value is not None:
            bits.append(f"odds 1:{g.odds_value:g}")
        extra = f" ({', '.join(bits)})" if bits else ""
        alerts.append(
            Alert(
                kind="new",
                game_number=num,
                name=g.name,
                severity=Severity.INFO,
                owned=False,
                message=f"🆕 New game now on sale: #{num} {g.name}{extra} — consider stocking it.",
                details={"price": g.price, "odds": g.odds, "on_sale_date": g.on_sale_date},
            )
        )

    # Sort: owned first, then by severity (critical -> info).
    sev_order = {Severity.CRITICAL: 0, Severity.WARN: 1, Severity.INFO: 2}
    alerts.sort(key=lambda a: (not a.owned, sev_order[a.severity], a.game_number))
    return alerts
