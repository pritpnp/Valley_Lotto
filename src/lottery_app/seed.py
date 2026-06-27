"""Initialize the app database and create the first store + owner.

Usage (from the repo root, with src on the path):

    PYTHONPATH=src python -m lottery_app.seed \
        --store "Valley Mart - Main St" \
        --username owner \
        --password "choose-a-strong-one" \
        --role admin \
        --import-config       # also load config.yaml's inventory into this store

Re-running is safe: an existing store (by slug) or user (by store+username) is
left untouched. Passwords are hashed with scrypt before they ever touch disk.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

# Make both packages importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lottery_app import db  # noqa: E402
from lottery_app.auth import hash_password  # noqa: E402


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "store"


def main(argv: list[str] | None = None) -> int:
    repo = Path(__file__).resolve().parents[2]
    ap = argparse.ArgumentParser(description="Seed the Valley Lotto app database.")
    ap.add_argument("--db", default=os.environ.get("LOTTO_DB", str(repo / "data" / "app.db")))
    ap.add_argument("--store", required=True, help="Display name of the store to create.")
    ap.add_argument("--username", required=True)
    ap.add_argument("--password", default=None, help="Omit to be prompted securely.")
    ap.add_argument("--role", default="admin", choices=["admin", "owner", "staff"])
    ap.add_argument("--import-config", action="store_true",
                    help="Load config.yaml's inventory into this store.")
    ap.add_argument("--config", default=str(repo / "config.yaml"))
    args = ap.parse_args(argv)

    password = args.password or getpass.getpass("Password for %s: " % args.username)
    if not password:
        print("A password is required.", file=sys.stderr)
        return 2

    conn = db.connect(args.db)
    db.init_db(conn)

    slug = _slug(args.store)
    store = db.get_store_by_slug(conn, slug)
    if store:
        store_id = store["id"]
        print(f"Store already exists: {args.store} (id={store_id})")
    else:
        store_id = db.create_store(conn, slug, args.store)
        print(f"Created store: {args.store} (id={store_id}, slug={slug})")

    if db.find_user(conn, store_id, args.username):
        print(f"User already exists: {args.username} @ {args.store} — left unchanged.")
    else:
        db.create_user(conn, store_id, args.username, hash_password(password), args.role)
        print(f"Created user: {args.username} (role={args.role}) @ {args.store}")

    if args.import_config:
        from lottery_tracker.config import Config

        cfg = Config.load(args.config)
        if cfg.inventory:
            existing = db.get_inventory(conn, store_id)
            db.set_inventory(conn, store_id, existing | cfg.inventory)
            print(f"Imported {len(cfg.inventory)} inventory games from {args.config}.")
        else:
            print(f"No inventory found in {args.config}.")

    conn.close()
    print("Done. Start the app with:\n"
          "  PYTHONPATH=src uvicorn lottery_app.main:app --host 0.0.0.0 --port 8000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
