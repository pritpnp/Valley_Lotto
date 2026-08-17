"""Guided count-session capture: walk the boxes in order, scan, auto-advance.

This is the state machine the scan page drives. A clerk runs a "count" by scanning
every box's top ticket in order — A1, A2, ... A24, B1, ... B24 — and the whole
walk takes about a minute. The session:

  * prompts one box at a time and **auto-advances** to the next after each scan,
  * lets you **correct mistakes mid-flow** without restarting the whole count:
      - :meth:`back` re-does the box you just scanned,
      - :meth:`rescan` fixes any box by name while keeping your place,
      - :meth:`goto` jumps the pointer to fill a skipped box,
  * **rejects** anything that isn't a valid ticket barcode (you stay on the box),
  * **warns** on a likely double-scan (the same ticket landing in two boxes),
  * on :meth:`commit`, appends every recorded scan to the durable log in one go.

The box *positions* are fixed (two 24-count units), but the **game in each box is
whatever you scan** — so this is not a locked planogram: the first count establishes
what's where, and every later count just updates it as products get swapped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .barcode import parse_ticket, BarcodeError, TicketCode
from .scans import Scan, ScanLog, load_scans, save_scans


def standard_slots(spec=(("", 48),)) -> list:
    """Build the ordered box labels. Default is plain 1..48; pass a lettered spec
    like ((\"A\",24),(\"B\",24)) for A1..A24, B1..B24."""
    out = []
    for letter, count in spec:
        out.extend(f"{letter}{i}" for i in range(1, count + 1))
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ScanStep:
    """The result of one scan attempt — what the UI shows and prompts next."""

    ok: bool
    slot: Optional[str]            # the box this scan was for
    message: str
    next_slot: Optional[str]       # box to prompt next (None => count complete)
    parsed: Optional[TicketCode] = None
    warning: Optional[str] = None  # non-fatal: recorded, but worth a second look

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "slot": self.slot, "message": self.message,
            "next_slot": self.next_slot, "warning": self.warning,
            "parsed": self.parsed.to_dict() if self.parsed else None,
        }


class CountSession:
    """An in-progress count. Feed it scans; commit when done."""

    def __init__(self, slots: Optional[list] = None, *, store: str = "default",
                 session: str = "morning", user: Optional[str] = None):
        self.slots: list = list(slots) if slots is not None else standard_slots()
        self.store = store
        self.session = session
        self.user = user
        self.entries: dict = {}     # slot -> Scan
        self.index = 0              # pointer into self.slots
        self.committed = False

    # --- where are we -----------------------------------------------------
    @property
    def current_slot(self) -> Optional[str]:
        return self.slots[self.index] if 0 <= self.index < len(self.slots) else None

    def pending(self) -> list:
        """Boxes not yet scanned, in order (catches any that were skipped)."""
        return [s for s in self.slots if s not in self.entries]

    def is_complete(self) -> bool:
        return not self.pending()

    def progress(self) -> tuple:
        return (len(self.entries), len(self.slots))

    def _advance(self) -> None:
        self.index += 1

    # --- scanning ---------------------------------------------------------
    def _record(self, slot: str, tc: TicketCode, at: str) -> Scan:
        scan = Scan.from_ticket(tc, scanned_at=at, kind="single", store=self.store,
                                slot=slot, session=self.session, user=self.user)
        self.entries[slot] = scan
        return scan

    def _dupe_warning(self, slot: str, tc: TicketCode) -> Optional[str]:
        # The exact same ticket (game+pack+ticket) in two different boxes almost
        # always means a mis-scan (wrong box, or scanned one ticket twice).
        for other, sc in self.entries.items():
            if other != slot and sc.game_number == tc.game_number \
                    and sc.pack == tc.pack and sc.ticket == tc.ticket:
                return f"same ticket as box {other} — did you scan the wrong box?"
        return None

    def scan(self, raw: str, *, at: Optional[str] = None) -> ScanStep:
        """Record a scan for the CURRENT box and auto-advance. On a bad barcode,
        stay put so the clerk can simply scan again."""
        slot = self.current_slot
        if slot is None:
            return ScanStep(False, None, "count already complete", None)
        try:
            tc = parse_ticket(raw)
        except BarcodeError as e:
            return ScanStep(False, slot, f"not a ticket barcode ({e}); scan {slot} again",
                            next_slot=slot)
        self._record(slot, tc, at or _now_iso())
        warning = self._dupe_warning(slot, tc)
        self._advance()
        nxt = self.current_slot
        msg = f"{slot} = game {tc.game_number}" + (f"  ➜ next: {nxt}" if nxt else "  ➜ done")
        return ScanStep(True, slot, msg, next_slot=nxt, parsed=tc, warning=warning)

    def back(self) -> ScanStep:
        """Step the pointer back one box to re-scan what you just did."""
        if self.index > 0:
            self.index -= 1
        slot = self.current_slot
        return ScanStep(True, slot, f"back to {slot}; scan it again", next_slot=slot)

    def goto(self, slot: str) -> ScanStep:
        """Jump the pointer to a specific box (e.g. to fill a skipped one)."""
        if slot not in self.slots:
            return ScanStep(False, None, f"unknown box {slot}", next_slot=self.current_slot)
        self.index = self.slots.index(slot)
        return ScanStep(True, slot, f"at {slot}; scan it", next_slot=slot)

    def skip(self) -> ScanStep:
        """Advance without recording the current box (fill it later via goto)."""
        slot = self.current_slot
        self._advance()
        return ScanStep(True, slot, f"skipped {slot}", next_slot=self.current_slot)

    def rescan(self, slot: str, raw: str, *, at: Optional[str] = None) -> ScanStep:
        """Fix ANY box by name without moving your place in the walk — this is the
        'go back and re-do one in the middle' operation."""
        if slot not in self.slots:
            return ScanStep(False, slot, f"unknown box {slot}", next_slot=self.current_slot)
        try:
            tc = parse_ticket(raw)
        except BarcodeError as e:
            return ScanStep(False, slot, f"not a ticket barcode ({e}); {slot} unchanged",
                            next_slot=self.current_slot)
        self._record(slot, tc, at or _now_iso())
        warning = self._dupe_warning(slot, tc)
        return ScanStep(True, slot, f"{slot} corrected = game {tc.game_number}",
                        next_slot=self.current_slot, parsed=tc, warning=warning)

    # --- serialization (so an in-progress count survives reloads) ---------
    def to_state(self) -> dict:
        return {
            "slots": self.slots, "store": self.store, "session": self.session,
            "user": self.user, "index": self.index, "committed": self.committed,
            "entries": {k: v.to_dict() for k, v in self.entries.items()},
        }

    @classmethod
    def from_state(cls, d: dict) -> "CountSession":
        s = cls(slots=d.get("slots"), store=d.get("store", "default"),
                session=d.get("session", "morning"), user=d.get("user"))
        s.index = int(d.get("index", 0))
        s.committed = bool(d.get("committed", False))
        s.entries = {k: Scan.from_dict(v) for k, v in (d.get("entries") or {}).items()}
        return s

    # --- finishing --------------------------------------------------------
    def scans_in_order(self) -> list:
        return [self.entries[s] for s in self.slots if s in self.entries]

    def commit(self, path) -> ScanLog:
        """Append every recorded scan (in box order) to the durable JSON log."""
        log = load_scans(path)
        for scan in self.finalize():
            log.add(scan)
        save_scans(path, log)
        return log

    def finalize(self) -> list:
        """Mark the count done and return its scans in box order. Used by storage
        backends (e.g. the database) that persist the scans themselves."""
        if self.committed:
            raise RuntimeError("this count session was already committed")
        scans = self.scans_in_order()
        self.committed = True
        return scans
