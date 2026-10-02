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

from dataclasses import dataclass, field
from enum import Enum

from .model import Game

# What decides the rating. One thing: how well the game sells.
RATING_FACTORS = ("sales",)


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
    """Where the line sits. A game rated under ``cutoff`` is a SEND BACK.

    The rating is how well a game sells across PA, against the typical game at
    its price: 100 × its sales a day ÷ the typical (median) sales a day of the
    games on sale at that price, capped at 100. So 100 = sells at least as well
    as the typical game at its price, and 20 = a fifth as well.

    The store earns 5% of every ticket sold and the lottery pays back what it
    pays out, so selling is what earns a box its place. Prizes left, odds and
    top prizes are shown with every game for information, but don't change the
    rating: what customers make of them already shows up in what they buy.
    """

    cutoff: float = 20.0         # rating below this → SEND BACK

    @classmethod
    def from_config(cls, cfg: dict | None) -> "RatingWeights":
        cfg = cfg or {}
        d = {}
        for f in cls.__dataclass_fields__:  # type: ignore[attr-defined]
            if f in cfg and cfg[f] is not None:
                d[f] = float(cfg[f])
        return cls(**d)


@dataclass
class Factor:
    """One fact about a game, as shown with its rating. ``weight`` is 100 for
    the one that decides the rating and 0 for those shown for information."""
    key: str
    label: str
    score: float | None     # 0..100, or None when there's no figure for it
    weight: float
    detail: str             # the value itself, short enough to sit in a column
    note: str = ""          # a full sentence saying what that value means


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def money(x: float) -> str:
    """Dollars a day, rounded the way a person would say them."""
    if x >= 100_000:
        return f"${x / 1000:,.0f}K"
    if x >= 10_000:
        return f"${x / 1000:,.1f}K".replace(".0K", "K")
    return f"${x:,.0f}"


def sales_vs_typical(game: Game) -> float | None:
    """This game's sales a day ÷ the typical game's at its price (1.0 = typical)."""
    s, t = game.sales_per_day, game.peer_typical_sales
    if s is None or not t:
        return None
    return s / t


def rate(game: Game, weights: "RatingWeights | None" = None) -> tuple[float | None, list[Factor]]:
    """Rate a game 0–100 on how well it sells (see ``RatingWeights``).

    Returns (rating, facts). ``rating`` is None when its sales couldn't be
    measured; the first fact says why. The typical sales at its price come from
    ``model.compare_with_peers``, which runs whenever games are loaded.
    """
    price = f"${game.price:g}" if game.price is not None else "this price"
    facts: list[Factor] = []

    ratio = sales_vs_typical(game)
    if ratio is not None:
        s, t = game.sales_per_day, game.peer_typical_sales
        pm = (f" (give or take {game.sales_uncertainty:.0%})"
              if game.sales_uncertainty is not None else "")
        facts.append(Factor(
            "sales", "Sales", _clamp(100 * ratio), 100.0, f"{money(s)} a day",
            f"Across Pennsylvania this game sells about {money(s)} of tickets a "
            f"day{pm}, measured from {game.sales_readings} of PA's weekly prize "
            f"counts over {game.sales_days:.0f} days. The typical {price} game "
            f"sells {money(t)} a day, so this one sells {ratio:.0%} as much."))
    else:
        facts.append(Factor("sales", "Sales", None, 100.0, "not measured",
                            game.sales_why or "Its sales couldn't be measured."))

    info = " For information: it doesn't change the rating."
    m = game.medium_pct_remaining
    facts.append(Factor(
        "prizes_left", "Prizes left", None if m is None else _clamp(100 * m), 0.0,
        "unknown" if m is None else f"{m:.0%}",
        (f"{m:.0%} of the prizes PA reports for this game are still out there, not "
         f"counting the top prize." + info) if m is not None else
        "PA's prize counts for this game couldn't be checked."))

    wb = game.win_back_odds
    facts.append(Factor(
        "win_back", "Wins more than it costs", None, 0.0,
        f"1 in {wb:.1f}" if wb else "unknown",
        (f"About one ticket in {wb:.1f} wins more than the {price} it cost." + info)
        if wb else "PA's printed prize list for this game couldn't be checked."))

    pair = game.top_prize_pair
    what = f" ({game.top_prize_value})" if game.top_prize_value else ""
    facts.append(Factor(
        "top_prizes", "Top prizes left", None, 0.0,
        f"{pair[0]:,} of {pair[1]:,}" if pair else "unknown",
        (f"{pair[0]:,} of the {pair[1]:,} top prizes{what} are still out there." + info)
        if pair else "PA's top-prize count for this game couldn't be matched."))

    o = game.odds_value
    facts.append(Factor(
        "odds", "Wins anything", None, 0.0, f"1 in {o:g}" if o else "unknown",
        (f"About one ticket in {o:g} wins something, even if it's just the price "
         f"of the ticket back." + info) if o else
        "PA hasn't published the odds for this game."))

    return (facts[0].score, facts)


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

    SEND BACK when PA has stopped selling it, or when its rating (how well it
    sells against the typical game at its price, see ``rate``) is under the
    cutoff. A game whose sales can't be measured yet stays, and says why.
    """
    if game.status == "ended":
        when = f" on {game.sales_end_date}" if game.sales_end_date else ""
        return ("send_back", f"Pennsylvania stopped selling this game{when}. Pull it.")

    w = weights or RatingWeights()
    score, facts = rate(game, w)
    if score is None:
        return ("keep", f"{facts[0].note} It stays until it can be.")
    ratio = sales_vs_typical(game)
    price = f"${game.price:g}" if game.price is not None else "this price"
    said = (f"Sells {money(game.sales_per_day)} a day across PA, {ratio:.0%} of the "
            f"typical {price} game ({money(game.peer_typical_sales)}).")
    if score < w.cutoff:
        return ("send_back", f"{said} Under {w.cutoff:.0f}% means send it back.")
    return ("keep", said)


def _is_low(game: Game, th: Thresholds) -> tuple[bool, list[str]]:
    """Return (is_low, reasons). A game is low if ANY configured rule trips."""
    reasons: list[str] = []
    pct = game.top_prize_pct_remaining
    pair = game.top_prize_pair
    if th.top_prize_pct is not None and pct is not None and pct < th.top_prize_pct:
        reasons.append(f"only {pct:.0%} of top prizes left ({pair[0]}/{pair[1]})")
    # Count floor: only meaningful once the game has actually started depleting.
    # A game that simply HAS one top prize and hasn't sold any sits at 100% — not
    # "low" — so we require remaining to be below the estimated original.
    depleting = pair is None or pair[0] < pair[1]
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
                    "top_prizes_total": (g.top_prize_pair or (None, None))[1],
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
