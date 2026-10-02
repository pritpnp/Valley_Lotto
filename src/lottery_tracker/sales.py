"""How fast each game sells across Pennsylvania, from PA's own prize counts.

A store earns 5% of every ticket it sells, and the lottery pays back what it
pays out, so a game is worth its box when it sells. PA doesn't publish sales,
but it does publish how many prizes are still unclaimed at each of a game's
top prize levels, and it updates those counts about once a week. Prizes are
spread evenly through the print run, so prizes being claimed tracks tickets
being sold:

    share of the game sold per day = prizes claimed per day ÷ prizes printed
    tickets per day               = that share × tickets printed
    dollars per day               = tickets per day × ticket price

"Prizes claimed per day" is the slope of a straight line fitted (least squares)
through the weekly counts of the last few weeks, summed over every published
level that has a printed count. The line's standard error, as a share of the
slope, is the ± shown with it.

Nothing is guessed. A game is measured only when:

* its printed prize table agrees with PA's other figures (the same check every
  other figure from that table passes), since it turns prizes into tickets;
* PA has posted at least ``MIN_READINGS`` different counts for it, spread over
  at least ``MIN_DAYS`` days, inside the last ``WINDOW_DAYS`` days;
* the counts go down. Counts that rise contradict themselves, and a line too
  ragged to read (± over ``MAX_UNCERTAINTY``) says nothing about speed.

Anything else is left unmeasured, and says why.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .model import Game, _money_to_num

WINDOW_DAYS = 35        # about five of PA's weekly updates
MIN_READINGS = 3        # a line through two points has no error to report
MIN_DAYS = 7
MAX_UNCERTAINTY = 0.5   # ± over half the figure: too ragged to read a speed from


@dataclass(frozen=True)
class Reading:
    """One set of counts as PA posted it: when we first saw it, and each game's
    prize levels {dollar value: prizes left}."""
    at: datetime
    left: dict          # game number -> {value_num: remaining}


@dataclass(frozen=True)
class Pace:
    dollars_per_day: Optional[float]   # None = not measured (see ``why``)
    tickets_per_day: Optional[float] = None
    uncertainty: Optional[float] = None  # standard error ÷ figure, e.g. 0.06 = ±6%
    readings: int = 0
    days: float = 0.0
    why: str = ""                      # set when not measured


def _when(captured_at: str) -> datetime:
    return datetime.fromisoformat(captured_at.replace("Z", "+00:00"))


def _levels(game: dict) -> dict:
    out = {}
    for t in game.get("prize_tiers") or []:
        v, rem = _money_to_num(t.get("value")), t.get("remaining")
        if v is not None and isinstance(rem, int):
            out[v] = rem
    return out


def extend(readings: list[Reading], snapshot: dict) -> bool:
    """Add one saved snapshot ({"captured_at", "games"}) to ``readings`` (kept
    oldest first) if it's a new posting from PA. Returns whether it was added.

    Our scraper runs twice a day but PA posts new counts about once a week, so
    most scrapes repeat the last posting. A repeat isn't new information and
    would make the line look surer than it is, so only the first sighting of
    each posting counts.
    """
    left = {str(n): _levels(g) for n, g in (snapshot.get("games") or {}).items()}
    sig = {n: lv for n, lv in left.items() if lv}
    if readings and sig == {n: lv for n, lv in readings[-1].left.items() if lv}:
        return False
    readings.append(Reading(_when(snapshot["captured_at"]), left))
    return True


def readings_from(snapshots: list[dict]) -> list[Reading]:
    """PA's distinct postings, oldest first, from saved snapshots in any order."""
    out: list[Reading] = []
    for snap in sorted(snapshots, key=lambda s: _when(s["captured_at"])):
        extend(out, snap)
    return out


def load_readings(snapshots_dir: str | Path, current: Optional[dict] = None) -> list[Reading]:
    """Readings from every saved snapshot, plus the run in hand (``current``,
    a {"captured_at", "games"} dict) if given. Unreadable files are skipped."""
    snaps = []
    d = Path(snapshots_dir)
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            raw = json.loads(f.read_text() or "{}")
        except (OSError, ValueError):
            continue
        if raw.get("captured_at") and raw.get("games"):
            snaps.append(raw)
    if current and current.get("captured_at"):
        snaps.append(current)
    return readings_from(snaps)


def _fit(xs: list[float], ys: list[float]) -> tuple[float, Optional[float]]:
    """Least-squares slope and its standard error (None with only two points)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    if n < 3:
        return b, None
    rss = sum((y - (my + b * (x - mx))) ** 2 for x, y in zip(xs, ys))
    return b, (rss / (n - 2) / sxx) ** 0.5


def measure(game: Game, readings: list[Reading]) -> Pace:
    """This game's sales speed over the last ``WINDOW_DAYS`` of ``readings``."""
    if not game.price:
        return Pace(None, why="PA hasn't given this game's ticket price.")
    if not game.printed_table_trusted:
        return Pace(None, why="PA's printed prize list for this game couldn't be checked "
                              "against its odds, so its prizes can't be turned into "
                              "tickets sold.")
    printed = {r["value_num"]: r["original"] for r in game.tier_health()
               if r["value_num"] is not None and r["original"]}
    if not printed or not readings:
        return Pace(None, why="PA doesn't list prize counts for this game.")

    end = readings[-1].at
    pts = []
    for r in readings:
        age = (end - r.at).total_seconds() / 86400
        if age > WINDOW_DAYS:
            continue
        lv = r.left.get(str(game.game_number)) or {}
        # The same prize levels in every reading, or the sums don't compare.
        if all(v in lv for v in printed):
            pts.append((-age, sum(lv[v] for v in printed)))
    days = pts[-1][0] - pts[0][0] if pts else 0.0
    if len(pts) < MIN_READINGS or days < MIN_DAYS:
        return Pace(None, readings=len(pts), days=days,
                    why=f"PA has posted {len(pts)} prize count{'s' if len(pts) != 1 else ''} "
                        f"for this game in the last {WINDOW_DAYS} days; it takes "
                        f"{MIN_READINGS}, at least {MIN_DAYS} days apart, to measure sales.")

    slope, se = _fit([p[0] for p in pts], [p[1] for p in pts])
    if slope > 0:
        return Pace(None, readings=len(pts), days=days,
                    why="PA's prize counts for this game went up, which can't happen "
                        "from sales, so its sales speed can't be read from them.")
    per_day = -slope / sum(printed.values())         # share of the game sold a day
    tickets = per_day * game.tickets_printed
    if slope == 0:
        return Pace(0.0, 0.0, 0.0, len(pts), days)
    unc = se / -slope if se is not None else None
    if unc is not None and unc > MAX_UNCERTAINTY:
        return Pace(None, readings=len(pts), days=days,
                    why="PA's prize counts for this game move too unevenly to read a "
                        "sales speed from them yet.")
    return Pace(tickets * game.price, tickets, unc, len(pts), days)


def attach(games: dict[str, Game], readings: list[Reading]) -> dict[str, Game]:
    """Measure every game on sale and record it on the game (saved with it)."""
    for g in games.values():
        p = measure(g, readings) if g.status == "active" else Pace(None)
        g.sales_per_day = None if p.dollars_per_day is None else round(p.dollars_per_day, 2)
        g.sales_uncertainty = None if p.uncertainty is None else round(p.uncertainty, 4)
        g.sales_readings = p.readings
        g.sales_days = round(p.days, 1)
        g.sales_why = p.why or None
    return games
