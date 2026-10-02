"""How fast each game sells in YOUR store, from your own counts.

PA-wide sales (sales.py) say how a game does across the state. Once a store has
counted for long enough, its own counts say how the game does on its own shelf,
which is what actually earns it. Then, for the games it carries, the rating
also compares each game with the store's own typical box at the same price, and
averages that with the PA-wide score (rules.rate).

A box's day is measured when it was counted at least twice that day (the
morning count to the night count is the day's sales). Per game:

    dollars a box a day = dollars sold on its measured box-days ÷ how many

The switch is all or nothing per game, and only on solid ground:

* the store has measured box-days on at least ``MIN_STORE_DAYS`` dates in the
  last ``WINDOW_DAYS`` days;
* the game has at least ``MIN_BOX_DAYS`` measured box-days;
* at least ``MIN_PEERS`` games at its price are measured in the store, so
  "typical" means something.

Anything short of that leaves the PA-wide figure alone. Nothing is guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Optional

WINDOW_DAYS = 28
MIN_STORE_DAYS = 14
MIN_BOX_DAYS = 7
MIN_PEERS = 3


@dataclass(frozen=True)
class StoreSales:
    """One game's sales in this store, and the store's typical box at its price."""
    per_box_day: float      # dollars a box a day
    typical: float          # the store's median dollars a box a day at this price
    box_days: int           # measured box-days behind ``per_box_day``
    days: int               # dates the store has measured, in the window
    price: Optional[float] = None


def measure(reports: list) -> dict[str, StoreSales]:
    """``reports``: the store's daily reports (reporting.DailyReport), one per
    date, for the last ``WINDOW_DAYS`` days. Returns {game number: StoreSales}
    for every game measured well enough to use."""
    dollars: dict = {}
    box_days: dict = {}
    price_of: dict = {}
    dates = set()
    for rep in reports:
        for row in rep.rows:
            if len(row.counts) < 2 or row.revenue is None or not row.price:
                continue                       # not counted twice, or no price: unmeasured
            g = str(row.game_number)
            dollars[g] = dollars.get(g, 0.0) + row.revenue
            box_days[g] = box_days.get(g, 0) + 1
            price_of[g] = row.price
            dates.add(rep.date)
    if len(dates) < MIN_STORE_DAYS:
        return {}

    per = {g: dollars[g] / box_days[g] for g in dollars if box_days[g] >= MIN_BOX_DAYS}
    by_price: dict = {}
    for g, v in per.items():
        by_price.setdefault(price_of[g], []).append(v)
    out = {}
    for g, v in per.items():
        peers = by_price[price_of[g]]
        if len(peers) < MIN_PEERS:
            continue
        out[g] = StoreSales(v, median(peers), box_days[g], len(dates), price_of[g])
    return out
