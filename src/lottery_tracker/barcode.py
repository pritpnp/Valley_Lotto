"""Decode the barcode printed on the back of a PA scratch-off ticket.

Every PA instant ticket carries a barcode in three segments::

    1750 - 0091798 - 010
     |        |        |
     |        |        +-- ticket number WITHIN the pack (0..pack_size-1)
     |        +----------- pack (a.k.a. "book") number: one physical pack of tickets
     +-------------------- game number (matches config.yaml inventory + the scraper)

A hand scanner hands us the *decoded* string, so we don't care what symbology
the barcode uses (Code 128, Interleaved 2 of 5, ...): whatever the gun reads, it
types out these digits. Real guns observed on this project reproduce the dashes
(``1750-0091798-010``); others strip them (``175000917980100``) and some append a
newline. ``parse_ticket`` tolerates all of those.

The two numbers that matter downstream:

* ``game_number`` — joins a scan to the tracked game (and its price/rating).
* ``(pack, ticket)`` — the position within a pack. Scanning the same pack at the
  start and end of a day gives a ticket-number delta = tickets sold that day.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict

# Segment widths in the canonical PA layout. Kept here (not hard-coded inline) so
# a different state/printer only needs these three numbers changed.
GAME_LEN = 4
PACK_LEN = 7
TICKET_LEN = 3
BARCODE_LEN = GAME_LEN + PACK_LEN + TICKET_LEN  # 14 digits, dashes aside

# The scanned barcode can carry MORE digits than the number printed on the ticket.
# Observed on a real PA ticket + retail scan gun:
#   printed on the back : 1750-0091798-010            (14 digits)
#   scan gun emits      : 1742011331200893            (16 digits)
#                         ^^^^ ^^^^^^^ ^^^ ^^
#                         game pack    tkt trailing
# The leading 14 carry game/pack/ticket; anything after is extra data the symbol
# encodes but the human-readable line omits (check digits / validation). We keep
# it verbatim in ``extra`` rather than discarding it, and never let its presence
# reject a scan.
# Ceiling on those trailing digits. 2 are confirmed in the field (16 total); this
# leaves headroom for a longer symbology without accepting arbitrary digit soup.
# A too-long scan fails loudly with a clear message, which beats silently
# mis-parsing it into wrong sales numbers.
MAX_EXTRA = 4  # accept 14..18 digits

_DASHED = re.compile(
    rf"^(?P<game>\d{{{GAME_LEN}}})[-\s]+(?P<pack>\d{{{PACK_LEN}}})[-\s]+"
    rf"(?P<ticket>\d{{{TICKET_LEN}}})(?:[-\s]*(?P<extra>\d{{1,{MAX_EXTRA}}}))?$"
)
_DIGITS_ONLY = re.compile(rf"^(?P<all>\d{{{BARCODE_LEN},{BARCODE_LEN + MAX_EXTRA}}})$")


class BarcodeError(ValueError):
    """Raised when a scanned string is not a recognizable ticket barcode."""


@dataclass(frozen=True)
class TicketCode:
    """A parsed ticket barcode. Immutable — it's a decoded fact, not state."""

    game_number: str   # normalized (no leading zeros) to match config/catalog keys
    pack: str          # kept zero-padded; it's an identifier, not a quantity
    ticket: int        # position within the pack, as an int for arithmetic
    raw: str           # exactly what the scanner emitted (for audit/debugging)
    extra: str = ""    # digits past the printed 14 (check/validation), kept verbatim

    @property
    def game_padded(self) -> str:
        """The game number as it appears in the barcode (zero-padded to GAME_LEN)."""
        return self.game_number.zfill(GAME_LEN)

    @property
    def pack_ticket(self) -> str:
        """A stable key for one physical pack, e.g. ``0091798`` — use to group scans."""
        return self.pack

    def to_dict(self) -> dict:
        return asdict(self)


def _clean(raw: str) -> str:
    """Strip the noise a scan gun can add: surrounding whitespace, a trailing
    Enter/Tab, and any wrapping quotes. Internal separators are handled by the
    regexes, so we only trim the edges here."""
    return raw.strip().strip("'\"").strip()


def parse_ticket(raw: str) -> TicketCode:
    """Parse a scanned string into a :class:`TicketCode`.

    Accepts the dashed form (``1750-0091798-010``), space-separated, or a bare
    digit run of 14+ digits (``17500091798010``, ``1742011331200893``). Digits past
    the printed 14 are kept in ``extra`` rather than rejected — real guns emit them.
    Raises :class:`BarcodeError` otherwise so callers can distinguish "not a ticket"
    (e.g. a pack/settlement barcode) from a real parse and surface a clear message
    instead of a wrong number.
    """
    if raw is None:
        raise BarcodeError("empty scan")
    s = _clean(raw)
    if not s:
        raise BarcodeError("empty scan")

    m = _DASHED.match(s)
    if m is None:
        # Fall back to a bare digit run, but only after removing separators so a
        # gun that strips dashes still parses. We do NOT blindly strip non-digits
        # from arbitrary input — we require a plausible length.
        compact = re.sub(r"[-\s]", "", s)
        m = _DIGITS_ONLY.match(compact)
        if m is None:
            raise BarcodeError(
                f"not a ticket barcode (need {BARCODE_LEN}+ digits): {raw!r}"
            )
        allnum = m.group("all")
        game = allnum[:GAME_LEN]
        pack = allnum[GAME_LEN:GAME_LEN + PACK_LEN]
        ticket = allnum[GAME_LEN + PACK_LEN:BARCODE_LEN]
        extra = allnum[BARCODE_LEN:]
    else:
        game, pack, ticket = m.group("game"), m.group("pack"), m.group("ticket")
        extra = m.group("extra") or ""

    game_norm = game.lstrip("0") or "0"  # "0993" -> "993"; matches un-padded config keys
    return TicketCode(game_number=game_norm, pack=pack, ticket=int(ticket),
                      raw=raw, extra=extra)


def try_parse_ticket(raw: str) -> TicketCode | None:
    """Non-raising variant: returns ``None`` instead of raising, for hot loops /
    UI where an unrecognized scan should be ignored rather than error out."""
    try:
        return parse_ticket(raw)
    except BarcodeError:
        return None
