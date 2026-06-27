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


def recommendation(game: Game, th: Thresholds) -> tuple[str, str]:
    """One clear call per game: ("keep" | "send_back", reason).

    SEND BACK (stop selling / return to PA) when the game can't earn for you:
      * sales have ended, OR
      * fewer than the threshold (default 40%) of its prizes are left, OR
      * its odds are weak AND its jackpots are already picked over.
    Otherwise KEEP it (with a note if it's getting weak so you don't reorder).
    """
    if game.status == "ended":
        when = f" {game.sales_end_date}" if game.sales_end_date else ""
        return ("send_back", f"sales ended{when} — pull it")

    # True "% of the whole game left": prefer the statistically-precise low-tier
    # sell-through; fall back to the top-prize % when we lack the prize structure.
    left = game.sell_through_pct if game.sell_through_pct is not None else game.top_prize_pct_remaining
    weak = (th.weak_odds is not None and game.odds_value is not None
            and game.odds_value > th.weak_odds)
    picked_over = game.jackpot_density is not None and game.jackpot_density < 0.85

    if left is not None and th.top_prize_pct is not None and left < th.top_prize_pct:
        return ("send_back", f"only {left:.0%} of prizes left")
    if weak and picked_over:
        return ("send_back", f"weak odds (1:{game.odds_value:g}) and jackpots picked over")

    notes = []
    if weak:
        notes.append(f"weak odds (1:{game.odds_value:g}) — fine while it sells, don't reorder")
    if picked_over:
        notes.append("jackpots thinning")
    return ("keep", "; ".join(notes) if notes else "good odds, prizes still available")


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
