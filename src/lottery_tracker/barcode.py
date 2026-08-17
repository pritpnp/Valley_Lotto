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

# Segment widths in the canonical PA layout — the *default hypothesis*, not an
# assumption. See CANDIDATE_LAYOUTS below: we never blindly slice the first 14
# digits, because a different store, state, or ticket printer can lay the fields
# out differently and a wrong split silently produces wrong sales numbers.
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
# Trailing digits are extra data the symbol encodes but the printed line omits
# (check/validation digits). Kept verbatim in ``extra``, never a reason to reject.
MAX_EXTRA = 4  # tolerate up to 4 trailing digits

# Field-width combinations we're willing to consider, widest-plausible first.
# (game, pack, ticket). PA is (4, 7, 3); the others cover other printers/states
# so this keeps working when you add a store somewhere else.
CANDIDATE_LAYOUTS = tuple(
    (g, p, t)
    for g in (4, 3, 5)      # game number width
    for p in (7, 6, 8)      # pack/book number width
    for t in (3,)           # ticket-within-pack width (3 digits everywhere seen)
)

# No pack holds more than a few hundred tickets (PA's largest is 300, the $1
# game). A "ticket number" above this means we split the digits in the wrong
# place — it is the single strongest structural check we have.
MAX_PLAUSIBLE_TICKET = 400

# A separator-delimited scan tells us the segmentation explicitly, so we trust
# its grouping (still validating the values). Widths are flexible here on purpose.
_DELIMITED = re.compile(
    r"^(?P<game>\d{3,5})[-\s]+(?P<pack>\d{5,9})[-\s]+"
    r"(?P<ticket>\d{2,4})(?:[-\s]*(?P<extra>\d{1,%d}))?$" % MAX_EXTRA
)


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
    # True/False when the caller supplied a catalog to check against, else None.
    # False means the digits split cleanly but the game number isn't a real game —
    # usually a misread or truncated scan, occasionally a game the scraper hasn't
    # picked up yet. Surfaced as a warning rather than a hard block.
    game_known: bool | None = None

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


def _norm_game(game: str) -> str:
    """'0993' -> '993'. Matches the un-padded keys used by config and the catalog."""
    return game.lstrip("0") or "0"


def _score(game: str, pack: str, ticket: str, extra: str,
           known_games=None) -> float | None:
    """How plausible is this segmentation? ``None`` means structurally impossible.

    Higher is better. The checks, strongest first:

    * **The game must exist.** When the caller supplies ``known_games`` (the PA
      catalog), a split whose game number is a real game is worth more than any
      combination of shape priors — this is what makes the parser portable to a
      different store or printer instead of trusting fixed offsets.
    * **The ticket must fit in a pack.** No pack holds >300 tickets, so a
      "ticket" of 893 proves the split is wrong.
    * Shape priors (PA's 4/7/3, a plain 2-digit check tail) only break ties.
    """
    tick = int(ticket)
    if tick > MAX_PLAUSIBLE_TICKET:
        return None                      # structurally impossible, not just unlikely
    if set(pack) == {"0"}:
        return None                      # pack 0000000 is not a real book

    score = 0.0
    g = _norm_game(game)

    if known_games:
        if g in known_games:
            score += 1000.0              # decisive: this really is a PA game number
        else:
            score -= 400.0               # suspicious, but not fatal (catalog can lag)

    # A ticket number well inside a real pack is a good sign.
    score += 20.0 if tick <= 300 else 5.0

    # Shape priors — tie-breakers only.
    score += {4: 8.0, 3: 2.0, 5: 2.0}.get(len(game), 0.0)
    score += {7: 6.0, 6: 2.0, 8: 2.0}.get(len(pack), 0.0)
    score += {0: 4.0, 2: 3.0}.get(len(extra), 0.0)   # no tail, or the observed 2
    if game.startswith("0"):
        score -= 3.0                     # PA game numbers aren't zero-padded in practice
    return score


def _best_split(digits: str, known_games=None):
    """Try every candidate layout and return the most plausible one."""
    best = None
    for g_len, p_len, t_len in CANDIDATE_LAYOUTS:
        core = g_len + p_len + t_len
        extra_len = len(digits) - core
        if extra_len < 0 or extra_len > MAX_EXTRA:
            continue
        game = digits[:g_len]
        pack = digits[g_len:g_len + p_len]
        ticket = digits[g_len + p_len:core]
        extra = digits[core:]
        s = _score(game, pack, ticket, extra, known_games)
        if s is None:
            continue
        if best is None or s > best[0]:
            best = (s, game, pack, ticket, extra)
    return best


def parse_ticket(raw: str, known_games=None) -> TicketCode:
    """Parse a scanned string into a :class:`TicketCode`.

    Rather than slicing fixed offsets, this tries the plausible field layouts and
    keeps the one whose *values* make sense — so it still works if another store,
    state, or ticket printer uses different widths.

    ``known_games``: an optional collection of real game numbers (the PA catalog).
    Supplying it makes parsing far more reliable, because a candidate split whose
    game number is an actual game beats one that merely has the expected shape.

    Accepts the delimited form (``1750-0091798-010``), space-separated, or a bare
    digit run (``17500091798010``, ``1742011331200893``). Raises
    :class:`BarcodeError` when nothing plausible fits, so a bad scan surfaces a
    clear message instead of a wrong number.
    """
    if raw is None:
        raise BarcodeError("empty scan")
    s = _clean(raw)
    if not s:
        raise BarcodeError("empty scan")

    known = {str(g) for g in known_games} if known_games else None

    # 1. A delimited scan states its own segmentation — trust the grouping, but
    #    still validate the values.
    m = _DELIMITED.match(s)
    if m is not None:
        game, pack = m.group("game"), m.group("pack")
        ticket, extra = m.group("ticket"), m.group("extra") or ""
        if _score(game, pack, ticket, extra, known) is not None:
            g = _norm_game(game)
            return TicketCode(game_number=g, pack=pack, ticket=int(ticket), raw=raw,
                              extra=extra, game_known=(g in known) if known else None)

    # 2. Otherwise infer the split from the digits by plausibility.
    digits = re.sub(r"[^0-9]", "", s)
    if len(digits) < min(sum(l) for l in CANDIDATE_LAYOUTS):
        raise BarcodeError(f"too short to be a ticket barcode: {raw!r}")

    best = _best_split(digits, known)
    if best is None:
        raise BarcodeError(
            f"no plausible game/pack/ticket split for {raw!r} "
            f"({len(digits)} digits) — is this a ticket barcode?"
        )
    _, game, pack, ticket, extra = best
    g = _norm_game(game)
    return TicketCode(game_number=g, pack=pack, ticket=int(ticket), raw=raw,
                      extra=extra, game_known=(g in known) if known else None)


def try_parse_ticket(raw: str, known_games=None) -> TicketCode | None:
    """Non-raising variant: returns ``None`` instead of raising, for hot loops /
    UI where an unrecognized scan should be ignored rather than error out."""
    try:
        return parse_ticket(raw, known_games)
    except BarcodeError:
        return None
