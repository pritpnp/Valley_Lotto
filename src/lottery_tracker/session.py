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
    needs_confirm: bool = False    # held back; scanning again accepts it

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "slot": self.slot, "message": self.message,
            "next_slot": self.next_slot, "warning": self.warning,
            "needs_confirm": self.needs_confirm,
            "parsed": self.parsed.to_dict() if self.parsed else None,
        }


class CountSession:
    """An in-progress count. Feed it scans; commit when done."""

    def __init__(self, slots: Optional[list] = None, *, store: str = "default",
                 session: str = "morning", user: Optional[str] = None,
                 known_games=None):
        self.slots: list = list(slots) if slots is not None else standard_slots()
        self.store = store
        self.session = session
        self.user = user
        # Real game numbers (the PA catalog), passed to the barcode parser so a
        # scan is validated against games that actually exist rather than trusted
        # by digit position. Not serialized — it's supplied fresh each request.
        self.known_games = set(known_games) if known_games else None
        self.entries: dict = {}     # slot -> Scan
        # A duplicate game held back until the clerk scans it a second time.
        self.pending: dict | None = None
        self.index = 0              # pointer into self.slots
        self.committed = False

    # --- where are we -----------------------------------------------------
    @property
    def current_slot(self) -> Optional[str]:
        return self.slots[self.index] if 0 <= self.index < len(self.slots) else None

    def pending_slots(self) -> list:
        """Boxes not yet scanned, in order (catches any that were skipped)."""
        return [s for s in self.slots if s not in self.entries]

    def is_complete(self) -> bool:
        return not self.pending_slots()

    @property
    def walk_done(self) -> bool:
        """True once the pointer has passed the last box.

        This — not "every box has a ticket" — is what ends a count, because a
        store legitimately has empty boxes that get skipped.
        """
        return self.index >= len(self.slots)

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

    def _scan_warning(self, slot: str, tc: TicketCode) -> Optional[str]:
        """Non-fatal things worth a second look. The scan is still recorded — a
        clerk mid-count shouldn't be blocked — but the UI shows the warning."""
        if tc.game_known is False:
            return (f"game {tc.game_number} isn't in the PA catalog — "
                    "check the scan (or it may be a brand-new game)")
        return self._dupe_warning(slot, tc)

    def _dupe_warning(self, slot: str, tc: TicketCode) -> Optional[str]:
        # The exact same ticket (game+pack+ticket) in two different boxes almost
        # always means a mis-scan (wrong box, or scanned one ticket twice).
        for other, sc in self.entries.items():
            if other != slot and sc.game_number == tc.game_number \
                    and sc.pack == tc.pack and sc.ticket == tc.ticket:
                return f"same ticket as box {other} — did you scan the wrong box?"
        return None

    def scan(self, raw: str, *, at: Optional[str] = None) -> ScanStep:
        """Record a scan for the CURRENT box and auto-advance.

        Two things are held back rather than silently accepted:

        * the **same ticket** appearing in a second box — physically impossible,
          so it means the gun fired twice or a box was missed;
        * the **same game** appearing in a second box — legitimate (a popular
          game is sometimes doubled up) but unusual enough to be worth
          confirming, which is done by simply scanning the ticket again.

        On a bad barcode we stay put so the clerk can just scan again.
        """
        slot = self.current_slot
        if slot is None:
            return ScanStep(False, None, "count already complete", None)
        try:
            tc = parse_ticket(raw, self.known_games)
        except BarcodeError as e:
            return ScanStep(False, slot, f"not a ticket barcode ({e}); scan {slot} again",
                            next_slot=slot)

        # The exact same ticket can't be in two boxes at once.
        for other, sc in self.entries.items():
            if other != slot and sc.pack == tc.pack and sc.ticket == tc.ticket:
                self.pending = None
                return ScanStep(
                    False, slot,
                    f"that's the same ticket you scanned for box {other} — "
                    f"scan the ticket from box {slot}",
                    next_slot=slot)

        # Same game already counted elsewhere: confirm by scanning again.
        dup_slot = next((other for other, sc in self.entries.items()
                         if other != slot and sc.game_number == tc.game_number), None)
        if dup_slot is not None:
            confirming = (self.pending or {}).get("slot") == slot and \
                         (self.pending or {}).get("game") == tc.game_number
            if not confirming:
                self.pending = {"slot": slot, "game": tc.game_number}
                return ScanStep(
                    False, slot,
                    f"Game {tc.game_number} was scanned in box {dup_slot}. "
                    f"Please scan the ticket again to confirm.",
                    next_slot=slot, parsed=tc, needs_confirm=True)

        self.pending = None
        self._record(slot, tc, at or _now_iso())
        warning = self._scan_warning(slot, tc)
        self._advance()
        nxt = self.current_slot
        msg = f"{slot} = game {tc.game_number}" + (f"  ➜ next: {nxt}" if nxt else "  ➜ done")
        return ScanStep(True, slot, msg, next_slot=nxt, parsed=tc, warning=warning)

    def back(self) -> ScanStep:
        """Step the pointer back one box to re-scan what you just did."""
        self.pending = None
        if self.index > 0:
            self.index -= 1
        slot = self.current_slot
        return ScanStep(True, slot, f"back to {slot}; scan it again", next_slot=slot)

    def goto(self, slot: str) -> ScanStep:
        """Jump the pointer to a specific box (e.g. to fill a skipped one)."""
        if slot not in self.slots:
            return ScanStep(False, None, f"unknown box {slot}", next_slot=self.current_slot)
        self.pending = None
        self.index = self.slots.index(slot)
        return ScanStep(True, slot, f"at {slot}; scan it", next_slot=slot)

    def skip(self) -> ScanStep:
        """Move past the current box, recording nothing for it.

        Anything already recorded for that box is dropped. Skipping means "this
        box is empty", and it has to be able to correct a scan as well as stand
        in for one — otherwise going back to a mis-scanned box and skipping it
        would leave the wrong ticket sitting there, which is the one outcome the
        clerk was trying to avoid.
        """
        slot = self.current_slot
        self.pending = None
        had = self.entries.pop(slot, None)
        self._advance()
        msg = (f"{slot} cleared — counted as empty" if had
               else f"skipped {slot} — counted as empty")
        return ScanStep(True, slot, msg, next_slot=self.current_slot)

    def clear(self, slot: str) -> ScanStep:
        """Mark any box empty by name, without moving your place in the walk.

        The counterpart to :meth:`rescan`: that fixes a box which holds the wrong
        ticket, this fixes a box which should hold nothing at all.
        """
        if slot not in self.slots:
            return ScanStep(False, slot, f"unknown box {slot}", next_slot=self.current_slot)
        self.pending = None
        had = self.entries.pop(slot, None)
        return ScanStep(True, slot,
                        f"{slot} is now empty" + ("" if had else " (it already was)"),
                        next_slot=self.current_slot)

    def set_entry(self, slot: str, *, game_number: str, pack: str, ticket: int,
                  at: Optional[str] = None) -> ScanStep:
        """Put values into a box by hand, without a barcode.

        The counterpart to scanning, for the times there is nothing to scan: a
        ticket number read off a paper sheet, or a digit mistyped and noticed
        later. Deliberately not validated against the barcode layouts — a person
        typing a correction knows more than the parser does.
        """
        if slot not in self.slots:
            return ScanStep(False, slot, f"unknown box {slot}", next_slot=self.current_slot)
        if not str(game_number).strip():
            return ScanStep(False, slot, "a box needs a game number",
                            next_slot=self.current_slot)
        try:
            ticket = int(ticket)
        except (TypeError, ValueError):
            return ScanStep(False, slot, "the ticket number has to be a number",
                            next_slot=self.current_slot)
        if ticket < 0:
            return ScanStep(False, slot, "the ticket number can't be negative",
                            next_slot=self.current_slot)

        self.pending = None
        self.entries[slot] = Scan(
            game_number=str(game_number).strip(), pack=str(pack or "").strip(),
            ticket=ticket, scanned_at=at or _now_iso(), kind="manual",
            store=self.store, slot=slot, session=self.session, user=self.user,
            raw="")
        return ScanStep(True, slot, f"{slot} set by hand to game {game_number}, "
                                    f"ticket {ticket:03d}",
                        next_slot=self.current_slot)

    def rescan(self, slot: str, raw: str, *, at: Optional[str] = None) -> ScanStep:
        """Fix ANY box by name without moving your place in the walk — this is the
        'go back and re-do one in the middle' operation."""
        if slot not in self.slots:
            return ScanStep(False, slot, f"unknown box {slot}", next_slot=self.current_slot)
        try:
            tc = parse_ticket(raw, self.known_games)
        except BarcodeError as e:
            return ScanStep(False, slot, f"not a ticket barcode ({e}); {slot} unchanged",
                            next_slot=self.current_slot)
        self._record(slot, tc, at or _now_iso())
        warning = self._scan_warning(slot, tc)
        return ScanStep(True, slot, f"{slot} corrected = game {tc.game_number}",
                        next_slot=self.current_slot, parsed=tc, warning=warning)

    # --- serialization (so an in-progress count survives reloads) ---------
    def to_state(self) -> dict:
        return {
            "slots": self.slots, "store": self.store, "session": self.session,
            "user": self.user, "index": self.index, "committed": self.committed,
            "pending": self.pending,
            "entries": {k: v.to_dict() for k, v in self.entries.items()},
        }

    @classmethod
    def from_state(cls, d: dict) -> "CountSession":
        s = cls(slots=d.get("slots"), store=d.get("store", "default"),
                session=d.get("session", "morning"), user=d.get("user"))
        s.index = int(d.get("index", 0))
        s.committed = bool(d.get("committed", False))
        s.pending = d.get("pending")
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
