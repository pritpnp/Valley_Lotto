"""Record ticket scans and turn open/close pairs into real sales.

This is the "countertop" half of the tracker. The scraper tells us how *desirable*
a game looks from PA's prize data; scanning tells us how a game actually *sells in
this store*. The workflow a clerk follows:

* **Open of day** — scan the next ticket to sell in each dispenser slot.
* **Close of day** — scan the next ticket again.

The ticket-number movement within the same pack is the number of tickets sold.
Multiplied by the ticket price (from the game catalog, not the barcode) that's the
day's revenue for the game. Accumulated over time it's a per-store sell-rate — the
truest "replace it if it doesn't sell" signal, and a future rating factor.

Vending machines don't need any of this: they just read the KEEP/RETURN rating.
So scanning is countertop-only by design.

Persistence mirrors ``state.py``: a JSON log under ``data/`` with dataclasses that
tolerate unknown keys, so older logs still load after we add fields.

Known limitation (documented, not hidden): sold-math is exact only *within one
pack*. When a pack runs out mid-day and a new pack is loaded, open and close carry
different pack numbers and a naive ticket delta is meaningless. Handling that needs
the pack size (tickets per pack, which varies by price point); until we capture it,
:func:`compute_sold` flags ``same_pack=False`` and refuses to guess.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .barcode import TicketCode, parse_ticket

# How a PA pack's ticket number moves as tickets sell. Not yet field-confirmed for
# every price point, so sold-math defaults to the absolute delta and records the
# observed direction; set this once we've confirmed it from real open/close data.
#   "up"   -> ticket number increases as tickets sell (open < close)
#   "down" -> ticket number decreases as tickets sell (open > close)
#   None   -> unknown; use abs(delta) and report which way it moved
COUNT_DIRECTION: Optional[str] = None


@dataclass
class Scan:
    """One physical scan event."""

    game_number: str
    pack: str
    ticket: int
    scanned_at: str                 # ISO timestamp, supplied by the caller (never invented here)
    kind: str = "single"            # "open" | "close" | "single"
    store: str = "default"          # store id, for multi-location rollups later
    user: Optional[str] = None      # who scanned, if the client knows
    raw: str = ""                   # exact scanner output, for audit

    @classmethod
    def from_ticket(cls, tc: TicketCode, *, scanned_at: str, kind: str = "single",
                    store: str = "default", user: Optional[str] = None) -> "Scan":
        return cls(
            game_number=tc.game_number, pack=tc.pack, ticket=tc.ticket,
            scanned_at=scanned_at, kind=kind, store=store, user=user, raw=tc.raw,
        )

    @classmethod
    def from_raw(cls, raw: str, *, scanned_at: str, kind: str = "single",
                 store: str = "default", user: Optional[str] = None) -> "Scan":
        """Parse a scanner string and build a Scan in one step."""
        return cls.from_ticket(parse_ticket(raw), scanned_at=scanned_at,
                               kind=kind, store=store, user=user)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Scan":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class SoldResult:
    """The tickets-sold / revenue outcome of pairing an open scan with a close scan."""

    game_number: str
    pack: str
    tickets_sold: Optional[int]     # None when it can't be computed (see same_pack)
    revenue: Optional[float]        # tickets_sold * price, or None if price unknown
    same_pack: bool                 # False => pack changed mid-period; sold is unreliable
    direction: str                  # "up" | "down" | "none" — how the ticket number moved
    open_ticket: int
    close_ticket: int
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def compute_sold(open_scan: Scan, close_scan: Scan,
                 *, price: Optional[float] = None) -> SoldResult:
    """Tickets sold between an open scan and a close scan of the same pack.

    ``price`` (the ticket's dollar price, from the game catalog) is optional; when
    given, ``revenue`` is filled in. Direction handling honors ``COUNT_DIRECTION``
    when set, otherwise falls back to the absolute delta and reports which way the
    number moved so a human can confirm the convention from real data.
    """
    if open_scan.pack != close_scan.pack:
        return SoldResult(
            game_number=close_scan.game_number, pack=close_scan.pack,
            tickets_sold=None, revenue=None, same_pack=False,
            direction="none", open_ticket=open_scan.ticket, close_ticket=close_scan.ticket,
            note=(f"pack changed during the period ({open_scan.pack} -> {close_scan.pack}); "
                  "sold count needs pack size to bridge the rollover"),
        )

    delta = close_scan.ticket - open_scan.ticket
    direction = "up" if delta > 0 else "down" if delta < 0 else "none"

    if COUNT_DIRECTION == "up":
        sold = delta
    elif COUNT_DIRECTION == "down":
        sold = -delta
    else:
        sold = abs(delta)

    note = ""
    if sold < 0:
        # Configured direction disagrees with the data — surface it, don't silently
        # emit a negative "sold". Most likely COUNT_DIRECTION is set the wrong way.
        note = (f"negative sold ({sold}) — COUNT_DIRECTION={COUNT_DIRECTION!r} may be "
                "inverted for this game, or open/close were swapped")

    revenue = None if price is None or sold < 0 else round(sold * price, 2)
    return SoldResult(
        game_number=close_scan.game_number, pack=close_scan.pack,
        tickets_sold=sold, revenue=revenue, same_pack=True,
        direction=direction, open_ticket=open_scan.ticket, close_ticket=close_scan.ticket,
        note=note,
    )


# --------------------------------------------------------------------------- #
# Persistence — an append-only scan log, same JSON style as state.py.
# --------------------------------------------------------------------------- #

@dataclass
class ScanLog:
    """An append-only list of scans, loadable/savable as JSON."""

    scans: list = field(default_factory=list)  # list[Scan]

    def add(self, scan: Scan) -> None:
        self.scans.append(scan)

    def for_pack(self, pack: str) -> list:
        return [s for s in self.scans if s.pack == pack]

    def to_dict(self) -> dict:
        return {"scans": [s.to_dict() for s in self.scans]}

    @classmethod
    def from_dict(cls, d: dict) -> "ScanLog":
        raw = d.get("scans", []) if isinstance(d, dict) else []
        return cls(scans=[Scan.from_dict(s) for s in raw])


def load_scans(path: str | Path) -> ScanLog:
    p = Path(path)
    if not p.exists():
        return ScanLog()
    return ScanLog.from_dict(json.loads(p.read_text() or "{}"))


def save_scans(path: str | Path, log: ScanLog) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(log.to_dict(), indent=2, sort_keys=True))
