"""How many tickets are in a physical pack (a.k.a. "book"), by ticket price.

PA builds packs to a fixed *dollar value*, not a fixed ticket count, so::

    tickets_per_pack = pack_value / ticket_price

Documented value tiers (from retailer/community sources; see the research notes
in the commit that added this file):

    $1, $2, $3, $5   -> $300 book
    $10, $20         -> $600 book
    $50              -> $2,500 book
    $30, $40         -> UNRESOLVED (fall in the gap; no reliable source)

Ticket numbering within a pack is **0-indexed**: tickets run ``000 .. N-1`` and
the printed number counts *up* as tickets sell. Our real sample confirms it —
game 1750 ($30) carried ticket ``010`` in pack ``0091798``, so a $30 pack holds
more than 10 tickets.

Confidence, straight from the research:
  * HIGH:   $2, $5, $10, $20  (directly stated and/or corroborated)
  * MEDIUM: $1, $3, $50       (unanimous but largely arithmetic from the value rule)
  * LOW:    $30, $40          (conflicting/absent sources — treat as placeholders)

Because the risky numbers ($30/$40) are exactly the ones we can't source, the
design does **not** trust the table blindly: :func:`pack_size_for` will override a
default the moment real scan data proves a game's pack is bigger than we assumed.
That makes each game self-correcting — a $30 game whose pack is really 30 (not
20) fixes itself once a clerk scans past ticket 020.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

# Tickets within a pack are numbered starting here (0 => first ticket is "000").
TICKET_INDEX_BASE = 0


@dataclass(frozen=True)
class PackSizeInfo:
    """A pack-size answer plus where it came from and how much to trust it."""

    size: Optional[int]
    confidence: str   # "high" | "medium" | "low" | "observed" | "override" | "unknown"
    source: str       # short human explanation

    def to_dict(self) -> dict:
        return {"size": self.size, "confidence": self.confidence, "source": self.source}


# Default tickets-per-pack by ticket price (dollars). Keyed by float price.
DEFAULT_PACK_SIZE_BY_PRICE: dict[float, PackSizeInfo] = {
    1.0:  PackSizeInfo(300, "medium", "$300 book / $1 (derived)"),
    2.0:  PackSizeInfo(150, "high",   "$300 book / $2 (stated)"),
    3.0:  PackSizeInfo(100, "medium", "$300 book / $3 (derived)"),
    5.0:  PackSizeInfo(60,  "high",   "$300 book / $5 (stated)"),
    10.0: PackSizeInfo(60,  "high",   "$600 book / $10 (stated)"),
    20.0: PackSizeInfo(30,  "high",   "$600 book / $20 (stated + corroborated)"),
    30.0: PackSizeInfo(20,  "low",    "$30 unresolved; $600/$30=20 best guess — verify from scans"),
    40.0: PackSizeInfo(15,  "low",    "$40 undocumented; $600/$40=15 placeholder — verify from scans"),
    50.0: PackSizeInfo(50,  "medium", "$2,500 book / $50 (stated)"),
}


def _price_key(price: Optional[float]) -> Optional[float]:
    """Normalize a price to a table key. Accepts 30, 30.0, "30", "$30"."""
    if price is None:
        return None
    if isinstance(price, str):
        price = price.strip().lstrip("$").replace(",", "")
    try:
        return float(price)
    except (TypeError, ValueError):
        return None


def observed_max_ticket(tickets: Iterable[int]) -> Optional[int]:
    """Highest ticket index seen (across a game's packs), or None if we have none.

    Because numbering is 0-indexed and counts up, ``max + 1`` is a *lower bound*
    on the true pack size: it's exact only if we've scanned at or near the last
    ticket of some pack. :func:`pack_size_for` uses it to correct — never to
    shrink — a default.
    """
    vals = [t for t in tickets if t is not None]
    return max(vals) if vals else None


def pack_size_for(
    price: Optional[float],
    *,
    observed_max: Optional[int] = None,
    override: Optional[int] = None,
) -> PackSizeInfo:
    """Best pack size for a game, reconciling three sources by trust:

    1. ``override`` — an explicit per-game number (config/manual). Always wins.
    2. ``observed_max`` — the highest ticket index seen in real scans. Wins over
       the default whenever it proves the pack is *bigger* than we assumed
       (``observed_max + 1 > default``); otherwise the default stands (we simply
       haven't scanned near the end yet).
    3. the price default table above.
    """
    if override is not None:
        return PackSizeInfo(int(override), "override", "configured per-game override")

    default = DEFAULT_PACK_SIZE_BY_PRICE.get(_price_key(price))
    observed_size = None if observed_max is None else observed_max - TICKET_INDEX_BASE + 1

    if observed_size is not None and (default is None or observed_size > default.size):
        return PackSizeInfo(
            observed_size, "observed",
            f"from scans: saw ticket {observed_max}, so pack holds ≥ {observed_size}",
        )
    if default is not None:
        return default
    return PackSizeInfo(None, "unknown", f"no default for price {price!r}; need a scan or override")
