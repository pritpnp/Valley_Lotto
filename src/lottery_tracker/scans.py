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

# How a PA pack's ticket number moves as tickets sell. Field-confirmed on this
# project: PA ticket numbers count UP as tickets are sold (open < close).
#   "up"   -> ticket number increases as tickets sell (open < close)
#   "down" -> ticket number decreases as tickets sell (open > close)
#   None   -> unknown; use abs(delta) and report which way it moved
COUNT_DIRECTION: Optional[str] = "up"


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
                 *, price: Optional[float] = None,
                 pack_size: Optional[int] = None) -> SoldResult:
    """Tickets sold between an open scan and a close scan of one dispenser slot.

    ``price`` (the ticket's dollar price, from the game catalog) is optional; when
    given, ``revenue`` is filled in. Direction handling honors ``COUNT_DIRECTION``
    when set, otherwise falls back to the absolute delta and reports which way the
    number moved so a human can confirm the convention from real data.

    ``pack_size`` (tickets per pack, from :mod:`.packs`) lets us bridge a *pack
    changeover*: if the slot rolled from one pack to the next between open and
    close, sold = (tickets left in the old pack) + (tickets sold from the new one).
    That bridge assumes the old pack sold out — the usual case when a slot is
    reloaded — and is reported as an estimate. Without ``pack_size`` a changeover
    can't be quantified, so we flag it rather than guess.
    """
    if open_scan.pack != close_scan.pack:
        if pack_size is not None:
            # Old pack: tickets open_ticket .. pack_size-1 sold out => (pack_size - open).
            # New pack: 0 .. close_ticket-1 sold => close_ticket. Single-rollover model.
            sold = (pack_size - open_scan.ticket) + close_scan.ticket
            revenue = None if price is None or sold < 0 else round(sold * price, 2)
            return SoldResult(
                game_number=close_scan.game_number, pack=close_scan.pack,
                tickets_sold=sold, revenue=revenue, same_pack=False,
                direction="up", open_ticket=open_scan.ticket, close_ticket=close_scan.ticket,
                note=(f"estimate across a pack changeover ({open_scan.pack} -> {close_scan.pack}), "
                      f"assuming the prior pack (size {pack_size}) sold out; "
                      "true count needs the actual last ticket of the old pack"),
            )
        return SoldResult(
            game_number=close_scan.game_number, pack=close_scan.pack,
            tickets_sold=None, revenue=None, same_pack=False,
            direction="none", open_ticket=open_scan.ticket, close_ticket=close_scan.ticket,
            note=(f"pack changed during the period ({open_scan.pack} -> {close_scan.pack}); "
                  "pass pack_size to bridge the rollover, or scan the old pack's last ticket"),
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

    def for_game(self, game_number: str) -> list:
        return [s for s in self.scans if s.game_number == str(game_number)]

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


# --------------------------------------------------------------------------- #
# Learning from the log: detect pack changes and learn pack size, so pack size
# never has to be configured up front — the scan history teaches it.
#
# The key facts that make this work:
#   * Every scan carries the pack ID, so a pack change is a FACT (the id differs),
#     not an inference. The ticket number resetting toward 0 corroborates it.
#   * Tickets count 0..N-1, so the highest ticket ever seen for a game is a lower
#     bound on its pack size N; once a pack is scanned near its end before it
#     rolls, that peak == N-1, i.e. learned size = peak + 1.
# --------------------------------------------------------------------------- #

def learn_pack_size(scans: list) -> Optional[int]:
    """Best-known pack size for one game, learned purely from its scans.

    Returns ``max(ticket) + 1`` over the given scans, or ``None`` if there are
    none. This is a *lower bound* that converges to the true size as tickets are
    scanned closer to a pack's end; it becomes exact once any pack of the game has
    been scanned at its final ticket before rolling to the next pack.
    """
    tickets = [s.ticket for s in scans if s.ticket is not None]
    return (max(tickets) + 1) if tickets else None


def learn_pack_sizes(log: "ScanLog") -> dict:
    """Learn a pack size for every game present in the log: ``{game_number: size}``."""
    by_game: dict[str, list] = {}
    for s in log.scans:
        by_game.setdefault(s.game_number, []).append(s)
    return {g: learn_pack_size(ss) for g, ss in by_game.items()}


@dataclass
class SequenceResult:
    """Tickets sold across a whole chronological run of scans for one game/slot,
    accounting for any pack changes along the way."""

    game_number: str
    tickets_sold: int
    revenue: Optional[float]
    pack_changes: int               # how many times the pack rolled over
    packs_seen: list                # ordered, de-duplicated pack ids
    pack_size_used: Optional[int]   # the size used to bridge rollovers (given or learned)
    estimated: bool                 # True if any rollover was bridged with an assumed size
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def sold_over_sequence(scans: list, *, price: Optional[float] = None,
                       pack_size: Optional[int] = None) -> SequenceResult:
    """Total tickets sold across a time-ordered run of scans for ONE game/slot.

    Walks consecutive scans. Within a pack, sold is the ticket delta (needs no
    pack size). At a pack change (different pack id), bridges the rollover using
    ``pack_size`` if given, else the size *learned from these very scans*
    (:func:`learn_pack_size`). If no size is available yet (e.g. the first-ever
    rollover for a brand-new game), the old pack's unseen tail can't be counted —
    that's noted, and only the new pack's portion is added.

    Scans are sorted by ``scanned_at`` (ISO strings sort chronologically).
    """
    ordered = sorted(scans, key=lambda s: s.scanned_at)
    if not ordered:
        return SequenceResult(game_number="", tickets_sold=0, revenue=None,
                              pack_changes=0, packs_seen=[], pack_size_used=None,
                              estimated=False, notes=["no scans"])

    game = ordered[0].game_number
    size = pack_size if pack_size is not None else learn_pack_size(ordered)

    sold = 0
    estimated = False
    pack_changes = 0
    packs_seen: list = []
    notes: list = []

    for s in ordered:
        if not packs_seen or packs_seen[-1] != s.pack:
            packs_seen.append(s.pack)

    for a, b in zip(ordered, ordered[1:]):
        if a.pack == b.pack:
            delta = b.ticket - a.ticket
            if delta < 0:
                notes.append(f"ticket went backwards within pack {a.pack} "
                             f"({a.ticket}->{b.ticket}); skipped")
                continue
            sold += delta
        else:
            pack_changes += 1
            new_portion = b.ticket  # tickets 0..b.ticket-1 sold from the new pack
            if size is not None and size > a.ticket:
                old_tail = size - a.ticket   # a.ticket..size-1 sold from the old pack
                sold += old_tail + new_portion
                estimated = True
            else:
                sold += new_portion
                notes.append(f"pack {a.pack}->{b.pack}: no learned size yet, "
                             f"old pack's tail after ticket {a.ticket} not counted")
                estimated = True

    revenue = None if price is None else round(sold * price, 2)
    return SequenceResult(
        game_number=game, tickets_sold=sold, revenue=revenue,
        pack_changes=pack_changes, packs_seen=packs_seen,
        pack_size_used=size, estimated=estimated, notes=notes,
    )
