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


# Built-in tickets-per-pack by ticket price (dollars), keyed by float price.
# This is an UNVERIFIED FALLBACK only — see the caveats in this module's docstring.
# The source of truth is ``config.yaml`` (``pack_sizes:``); anything you set there
# overrides these. Prefer to confirm the real numbers from the PA retailer
# "Settled Packs" report and put them in config.
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


class PackSizeResolver:
    """Resolve a game's pack size from config first, then scans, then the fallback.

    Configuration lives in ``config.yaml`` under ``pack_sizes:`` and is the
    authoritative source — the built-in :data:`DEFAULT_PACK_SIZE_BY_PRICE` is only
    consulted when config says nothing and ``use_builtin_fallback`` is on.

    Resolution order (most specific / most trusted first):
      1. ``by_game`` — an explicit size you set for one game number. Authoritative.
      2. real scans — a scanned ticket index proves a *lower bound*; it corrects a
         per-price number *upward* if the pack is demonstrably bigger (you can't
         have ticket 028 in a 20-ticket pack). It never shrinks a configured size.
      3. ``by_price`` — your per-price default from config.
      4. the built-in fallback table (unverified), if enabled.
    """

    def __init__(self, by_price: dict | None = None, by_game: dict | None = None,
                 *, use_builtin_fallback: bool = True):
        self.by_price = {}
        for k, v in (by_price or {}).items():
            key = _price_key(k)
            if key is not None and v is not None:
                self.by_price[key] = int(v)
        self.by_game = {str(k).strip(): int(v) for k, v in (by_game or {}).items() if v is not None}
        self.use_builtin_fallback = use_builtin_fallback

    def size_for(self, price: Optional[float], *, game_number: Optional[str] = None,
                 observed_max: Optional[int] = None) -> PackSizeInfo:
        # 1. explicit per-game size — what you set wins.
        if game_number is not None and str(game_number).strip() in self.by_game:
            size = self.by_game[str(game_number).strip()]
            if observed_max is not None and (observed_max + 1) > size:
                # A scan physically contradicts the configured size — trust reality
                # and make the discrepancy loud so config can be fixed.
                corrected = observed_max - TICKET_INDEX_BASE + 1
                return PackSizeInfo(
                    corrected, "observed",
                    f"config says {size} for game {game_number}, but a scan hit ticket "
                    f"{observed_max}; using {corrected} — check config.yaml",
                )
            return PackSizeInfo(size, "config-game", f"per-game size from config.yaml (game {game_number})")

        # 2. base default: per-price config, else the built-in fallback.
        key = _price_key(price)
        if key in self.by_price:
            base: Optional[PackSizeInfo] = PackSizeInfo(
                self.by_price[key], "config-price", f"per-price size from config.yaml (${key:g})")
        elif self.use_builtin_fallback:
            base = DEFAULT_PACK_SIZE_BY_PRICE.get(key)
        else:
            base = None

        # 3. correct upward from real scans.
        observed_size = None if observed_max is None else observed_max - TICKET_INDEX_BASE + 1
        if observed_size is not None and (base is None or observed_size > base.size):
            return PackSizeInfo(
                observed_size, "observed",
                f"from scans: saw ticket {observed_max}, so pack holds ≥ {observed_size}")
        if base is not None:
            return base
        return PackSizeInfo(None, "unknown", f"no config or fallback for price {price!r}")
