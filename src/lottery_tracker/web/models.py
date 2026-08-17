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
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="clerk")   # "clerk" | "admin"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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
