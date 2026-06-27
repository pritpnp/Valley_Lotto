"""SQLite storage for the multi-store app.

Three tables, one shared database:

  * ``stores``          — one row per physical store (slug + display name).
  * ``users``           — logins; each belongs to a store. role ∈
                          {'admin','owner','staff'}. 'admin' can see/manage every
                          store; 'owner' manages its own store's users + inventory;
                          'staff' edits its own store's inventory.
  * ``store_inventory`` — which PA game numbers each store currently carries.

The PA scratch-off catalog itself is NOT stored here — it lives in the scraper's
``data/state.json`` and is shared read-only by every store (see ``pa_data.py``).
This keeps one source of truth for the lottery data and a tiny per-store table for
"who carries what".
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id      INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    username      TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'staff',
    created_at    TEXT NOT NULL,
    UNIQUE (store_id, username)
);

CREATE TABLE IF NOT EXISTS store_inventory (
    store_id    INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    game_number TEXT NOT NULL,
    added_at    TEXT NOT NULL,
    PRIMARY KEY (store_id, game_number)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with sane defaults (row dicts + foreign keys on)."""
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# --- stores -----------------------------------------------------------------
def create_store(conn: sqlite3.Connection, slug: str, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO stores (slug, name, created_at) VALUES (?, ?, ?)",
        (slug, name, now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_stores(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM stores ORDER BY name").fetchall()


def get_store(conn: sqlite3.Connection, store_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()


def get_store_by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM stores WHERE slug = ?", (slug,)).fetchone()


# --- users ------------------------------------------------------------------
def create_user(
    conn: sqlite3.Connection, store_id: int, username: str, password_hash: str,
    role: str = "staff",
) -> int:
    cur = conn.execute(
        "INSERT INTO users (store_id, username, password_hash, role, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (store_id, username, password_hash, role, now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def find_user(conn: sqlite3.Connection, store_id: int, username: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE store_id = ? AND username = ?", (store_id, username)
    ).fetchone()


def list_users(conn: sqlite3.Connection, store_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM users WHERE store_id = ? ORDER BY username", (store_id,)
    ).fetchall()


def set_password(conn: sqlite3.Connection, user_id: int, password_hash: str) -> None:
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
    conn.commit()


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()


# --- inventory --------------------------------------------------------------
def get_inventory(conn: sqlite3.Connection, store_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT game_number FROM store_inventory WHERE store_id = ?", (store_id,)
    ).fetchall()
    return {r["game_number"] for r in rows}


def add_inventory(conn: sqlite3.Connection, store_id: int, game_number: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO store_inventory (store_id, game_number, added_at) "
        "VALUES (?, ?, ?)",
        (store_id, game_number, now_iso()),
    )
    conn.commit()


def remove_inventory(conn: sqlite3.Connection, store_id: int, game_number: str) -> None:
    conn.execute(
        "DELETE FROM store_inventory WHERE store_id = ? AND game_number = ?",
        (store_id, game_number),
    )
    conn.commit()


def set_inventory(conn: sqlite3.Connection, store_id: int, game_numbers: set[str]) -> None:
    """Replace a store's whole inventory set in one transaction."""
    conn.execute("DELETE FROM store_inventory WHERE store_id = ?", (store_id,))
    ts = now_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO store_inventory (store_id, game_number, added_at) VALUES (?, ?, ?)",
        [(store_id, gn, ts) for gn in game_numbers],
    )
    conn.commit()
