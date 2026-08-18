"""Schema upkeep for a database that already has live data.

``Base.metadata.create_all`` only creates *missing tables* — it will never add a
column to a table that already exists. The deployed app has real accounts,
inventory and scans in Postgres, so introducing multi-store required a small,
explicit migration rather than a model change alone.

Deliberately minimal (no Alembic): additive only, safe to run on every boot, and
tolerant of being interrupted. It never drops or rewrites data.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from .models import Base, Store, User

log = logging.getLogger(__name__)

# Columns added to tables that predate multi-store. (table, column, DDL type)
_ADDED_COLUMNS = [
    ("users", "username", "VARCHAR(64)"),
    ("users", "store", "VARCHAR(64)"),
    ("users", "display_name", "VARCHAR(128)"),
    ("users", "active", "BOOLEAN"),
]


def _existing_columns(engine, table: str) -> set:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def add_missing_columns(engine) -> list:
    """ALTER TABLE ... ADD COLUMN for anything the models gained. Returns what ran."""
    done = []
    for table, column, ddl in _ADDED_COLUMNS:
        cols = _existing_columns(engine, table)
        if not cols or column in cols:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            done.append(f"{table}.{column}")
        except Exception as e:  # noqa: BLE001 — a failed add must not stop boot
            log.warning("could not add %s.%s: %s", table, column, e)
    return done


def relax_legacy_not_null(engine) -> None:
    """Accounts are created with a username now, so the old NOT NULL on
    ``users.email`` would reject them. SQLite can't drop a NOT NULL in place, but
    a SQLite database here is only ever a fresh dev one, which already has the
    nullable definition."""
    if engine.dialect.name != "postgresql":
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ALTER COLUMN email DROP NOT NULL"))
    except Exception as e:  # noqa: BLE001
        log.debug("email NOT NULL not relaxed (probably already nullable): %s", e)


def backfill(engine, default_slug: str, default_name: str,
             timezone: str = "America/New_York", slots: str = "48") -> dict:
    """Give existing data a home in the new multi-store world.

    * Create a Store row for the slug the single-store deployment was using, so
      its inventory, staff, scans and emphasis (all already tagged with that
      slug) belong to a real store.
    * Give legacy accounts a username derived from their email, and promote the
      first one to superadmin — otherwise the owner could not reach the new
      admin pages after deploying.
    """
    out = {"store_created": False, "usernames": 0, "superadmin": None}
    with Session(engine) as db:
        if db.get(Store, default_slug) is None:
            db.add(Store(slug=default_slug, name=default_name,
                         timezone=timezone, slots=slots))
            out["store_created"] = True

        users = db.scalars(select(User).order_by(User.id)).all()
        for u in users:
            if not u.username and u.email:
                u.username = u.email.split("@")[0][:64]
                out["usernames"] += 1
            if u.active is None:
                u.active = True
        if users and not any(u.role == "superadmin" for u in users):
            users[0].role = "superadmin"
            users[0].store = None          # a superadmin belongs to no single store
            out["superadmin"] = users[0].username or users[0].email
        db.commit()
    return out


def enable_rls(engine) -> list:
    """Turn on Row Level Security for our tables, on Postgres only.

    Why this matters on Supabase: every table in the ``public`` schema is also
    served by Supabase's auto-generated REST API. Without RLS, anyone holding the
    project's anon key could read or write store data directly, bypassing this
    app entirely — the app's own permission checks would be irrelevant.

    Enabling RLS **with no policies** denies all access to the API roles (anon,
    authenticated), which is exactly what we want: this data should only ever be
    reached through the app.

    It does not lock the app out, because a table's OWNER bypasses RLS, and the
    app owns these tables (it created them). We deliberately do NOT use FORCE ROW
    LEVEL SECURITY, which would subject the owner to the policies too and — with
    no policies defined — would cut the app off from its own data.

    Runs on every boot so a table added later can't be left unprotected.
    """
    if engine.dialect.name != "postgresql":
        return []
    done = []
    for table in sorted(Base.metadata.tables):
        try:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
            done.append(table)
        except Exception as e:  # noqa: BLE001 — never block boot on this
            log.warning("could not enable RLS on %s: %s", table, e)
    return done


def ensure_schema(engine, *, default_slug: str, default_name: str,
                  timezone: str = "America/New_York", slots: str = "48") -> dict:
    """Create tables, add new columns, then backfill. Safe on every boot."""
    Base.metadata.create_all(engine)
    added = add_missing_columns(engine)
    if added:
        log.info("schema: added columns %s", ", ".join(added))
    relax_legacy_not_null(engine)
    result = backfill(engine, default_slug, default_name, timezone, slots)
    result["added_columns"] = added
    result["rls_enabled"] = enable_rls(engine)
    return result
