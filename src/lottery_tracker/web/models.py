"""Database models. Works on SQLite (local dev) and Postgres/Supabase (prod) —
the only difference is the ``DATABASE_URL``. Tables auto-create on first boot, so
there's no manual SQL to run against Supabase.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import String, Integer, Float, Boolean, DateTime, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """A login account: superadmin or a store manager.

    Employees are NOT users — they're :class:`StaffRow` rows who unlock a shift
    with a PIN on a device the manager already signed in. That keeps passwords
    off the counter while still naming who ran each count.

    ``store`` is a store slug (``stores.slug``) and is NULL for a superadmin,
    who is not tied to any one location.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), unique=True, index=True,
                                                 nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True,
                                              nullable=True)   # legacy accounts
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="manager")  # superadmin|manager
    store: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    @property
    def is_superadmin(self) -> bool:
        return self.role == "superadmin"

    @property
    def label(self) -> str:
        return self.display_name or self.username or self.email or f"user{self.id}"


class Store(Base):
    """One physical location. Every other table already carries a ``store`` slug,
    so this table adds the profile without touching existing rows."""

    __tablename__ = "stores"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))            # "Valley Mart WB"
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    slots: Mapped[str] = mapped_column(String(64), default="48")   # box layout spec
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retailer_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    @property
    def title(self) -> str:
        """What the header shows for this store's people: 'Valley Mart WB Lottery'.

        A name that already ends in Lotto/Lottery is left alone — otherwise the
        default store, carried over from the single-store install, would read
        "Valley Lotto Lottery".
        """
        name = (self.name or "").strip()
        if name.lower().endswith(("lottery", "lotto")):
            return name
        return f"{name} Lottery"


class AuditRow(Base):
    """Who changed what, across every store — the accountability trail.

    Deliberately separate from :class:`AccessRow` (which is raw traffic): this
    records *actions* in business terms, so it stays readable months later.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    store: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    actor: Mapped[str] = mapped_column(String(128), default="")     # username or staff name
    actor_role: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)     # "inventory.add", ...
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


class ScanRow(Base):
    """One committed scan. Mirrors :class:`lottery_tracker.scans.Scan` plus who/when."""

    __tablename__ = "scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store: Mapped[str] = mapped_column(String(64), index=True, default="default")
    game_number: Mapped[str] = mapped_column(String(16), index=True)
    pack: Mapped[str] = mapped_column(String(32), index=True)
    ticket: Mapped[int] = mapped_column(Integer)
    slot: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    session: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scanned_at: Mapped[str] = mapped_column(String(32), index=True)   # ISO string, as scanned
    user_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw: Mapped[str] = mapped_column(Text, default="")

    def to_scan(self):
        """Convert to the pure-engine :class:`Scan` dataclass for reporting/learning."""
        from ..scans import Scan
        return Scan(game_number=self.game_number, pack=self.pack, ticket=self.ticket,
                    scanned_at=self.scanned_at, store=self.store, slot=self.slot,
                    session=self.session, user=self.user_email, raw=self.raw)


class InventoryRow(Base):
    """A game this store currently carries. Mirrors lottery_app's store_inventory,
    but keyed by the store slug so it works without the FastAPI app's store table."""

    __tablename__ = "store_inventory"
    __table_args__ = (UniqueConstraint("store", "game_number", name="uq_inv_store_game"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store: Mapped[str] = mapped_column(String(64), index=True, default="default")
    game_number: Mapped[str] = mapped_column(String(16), index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class EmphasisRow(Base):
    """Per-store emphasis sliders for the KEEP/SEND-BACK rating.

    One notch multiplies that factor's base weight by 1.6**notch (see
    ``RatingWeights.scaled``). 0 = neutral; the UI clamps to -3..+3.
    """

    __tablename__ = "store_emphasis"

    store: Mapped[str] = mapped_column(String(64), primary_key=True)
    odds: Mapped[float] = mapped_column(Float, default=0.0)
    prizes_left: Mapped[float] = mapped_column(Float, default=0.0)
    low_prize: Mapped[float] = mapped_column(Float, default=0.0)
    low_prize_skew: Mapped[float] = mapped_column(Float, default=0.0)
    jackpot_density: Mapped[float] = mapped_column(Float, default=0.0)

    def to_emphasis(self) -> dict:
        return {f: float(getattr(self, f)) for f in
                ("odds", "prizes_left", "low_prize", "low_prize_skew", "jackpot_density")}


class ActiveCount(Base):
    """The in-progress count for a (store, user), stored as JSON so a page reload
    or server restart never loses a half-finished walk of the boxes."""

    __tablename__ = "active_counts"
    __table_args__ = (UniqueConstraint("store", "user_email", name="uq_active_store_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store: Mapped[str] = mapped_column(String(64), default="default")
    user_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    state_json: Mapped[str] = mapped_column(Text)   # CountSession.to_state() as JSON
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class StaffRow(Base):
    """A person who works at a store, unlocked by a short PIN.

    Two layers on purpose: the *device* stays signed in to the store account
    (email + password, entered once), and each person unlocks their shift with a
    PIN. That keeps a counter tablet usable all day without anyone typing a
    password, while still attributing every scan to a named person.

    The PIN is hashed, never stored in the clear. PINs are short by nature, so
    they are only ever accepted for staff *within one store* and are not a
    substitute for the store password — you cannot reach the site with a PIN
    alone.
    """

    __tablename__ = "staff"
    __table_args__ = (UniqueConstraint("store", "name", name="uq_staff_store_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store: Mapped[str] = mapped_column(String(64), index=True, default="default")
    name: Mapped[str] = mapped_column(String(64))
    pin_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="clerk")   # "clerk" | "manager"
    # Comma-separated capability keys (see web/permissions.py). Empty means this
    # person only runs counts. A staff "manager" holds everything regardless, so
    # this column is what gives an ordinary employee a slice of manager work —
    # receiving shipments, say — without the rest of it.
    permissions: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    def granted(self) -> set:
        from .permissions import parse, ALL_KEYS
        if (self.role or "") in ("manager", "supervisor"):
            return set(ALL_KEYS)
        return parse(self.permissions)


class AccessRow(Base):
    """Who reached the site, from where.

    Exists because the front door can be run PIN-only (no store password), which
    trades security for convenience. An access log is the compensating control:
    you can see every device that touched the site and every PIN attempt,
    including the failures that a password would otherwise have stopped.

    One row per request (health checks and static files excluded), so this table
    grows steadily — prune it periodically if it ever gets unwieldy.
    """

    __tablename__ = "access_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    store: Mapped[str] = mapped_column(String(64), index=True, default="default")
    ip: Mapped[str] = mapped_column(String(64), index=True, default="")
    method: Mapped[str] = mapped_column(String(8), default="GET")
    path: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[int] = mapped_column(Integer, default=0)
    # "page" for ordinary traffic; "pin_ok" / "pin_fail" for sign-in attempts,
    # which are the rows worth watching.
    event: Mapped[str] = mapped_column(String(24), default="page", index=True)
    staff_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str] = mapped_column(String(255), default="")


class BoxRow(Base):
    """Which game sits in which dispenser box.

    "We carry these 34 games" is only half the picture — a store has N physical
    boxes and each holds one game. Modelling the boxes means the dashboard can
    say *where* a dead game is sitting, and a count knows what it should be
    seeing in each slot.

    Kept deliberately self-maintaining: every scan already carries its slot and
    (from the barcode) its game, so committing a count updates this map. Manual
    editing is for corrections, not data entry.
    """

    __tablename__ = "store_boxes"
    __table_args__ = (UniqueConstraint("store", "slot", name="uq_box_store_slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store: Mapped[str] = mapped_column(String(64), index=True, default="default")
    slot: Mapped[str] = mapped_column(String(16))          # "1".."48" or "A1"
    game_number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # "scan" when a count filled it in, "manual" when a person set it — so the
    # UI can show which boxes are actually confirmed by a ticket.
    source: Mapped[str] = mapped_column(String(16), default="manual")


class ShipmentRow(Base):
    """One delivery of unopened packs.

    The shipping label is scanned and kept verbatim (whatever the courier's
    barcode says) so a pack can always be traced back to the day it arrived —
    which is the difference between "we're low on this game" and "we're low
    because the last delivery never included it".
    """

    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store: Mapped[str] = mapped_column(String(64), index=True, default="default")
    label: Mapped[str] = mapped_column(Text, default="")        # scanned shipping label
    received_on: Mapped[str] = mapped_column(String(16), index=True, default="")  # local date
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    received_by: Mapped[str] = mapped_column(String(128), default="")
    note: Mapped[str] = mapped_column(Text, default="")


class PackRow(Base):
    """One physical pack of tickets, followed from delivery to return.

    A pack is the unit a store actually buys, stocks, opens and settles — the
    thing the ticket barcode's middle field identifies. Tracking it is what makes
    three questions answerable that box scans alone cannot answer:

      * what is sitting unopened in the back room right now,
      * whether a pack that left backstock ever showed up in a box (and if a box
        burned through several packs between two counts, how many),
      * which packs were settled and returned, when, by whom and why.

    ``state`` moves backstock -> active -> settled, and a pack can be settled
    straight out of backstock without ever being opened.
    """

    __tablename__ = "packs"
    __table_args__ = (UniqueConstraint("store", "game_number", "pack", name="uq_pack_store_game"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store: Mapped[str] = mapped_column(String(64), index=True, default="default")
    game_number: Mapped[str] = mapped_column(String(16), index=True)
    pack: Mapped[str] = mapped_column(String(32), index=True)
    # backstock = unopened in the back room; active = in a box; sold_out = the
    # box moved on to another pack; settled = taken out of play and returned.
    state: Mapped[str] = mapped_column(String(16), index=True, default="backstock")

    shipment_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    received_on: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The last night the pack was seen unopened, so an absence can be dated.
    last_seen_on: Mapped[str | None] = mapped_column(String(16), nullable=True)
    opened_on: Mapped[str | None] = mapped_column(String(16), nullable=True)
    slot: Mapped[str | None] = mapped_column(String(16), nullable=True)   # box it went into

    settled_on: Mapped[str | None] = mapped_column(String(16), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    settle_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    settle_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # "scan" when a shipment or backstock count put it here, "inferred" when it
    # was deduced (e.g. a box scan showed a pack nobody had recorded receiving).
    source: Mapped[str] = mapped_column(String(16), default="scan")

    @property
    def is_held(self) -> bool:
        """Unopened and still ours — the thing "backstock" means."""
        return self.state == "backstock"
