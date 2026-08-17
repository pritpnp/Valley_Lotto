"""Database models. Works on SQLite (local dev) and Postgres/Supabase (prod) —
the only difference is the ``DATABASE_URL``. Tables auto-create on first boot, so
there's no manual SQL to run against Supabase.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import String, Integer, Float, DateTime, Text, UniqueConstraint
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
