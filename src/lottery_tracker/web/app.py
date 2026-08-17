"""Flask app: per-user login + the guided scan-capture page + daily report.

Runs on SQLite locally and Postgres/Supabase in production — set ``DATABASE_URL``.
Start it with ``python -m lottery_tracker.web`` (dev) or gunicorn (prod, see Procfile).

Environment variables (all optional in dev; see SETUP.md for prod):
  DATABASE_URL   sqlite:///.../data/valley.db  (default)  or  postgresql://...(Supabase)
  SECRET_KEY     signs the login cookie (a dev default is used if unset, with a warning)
  REGISTER_CODE  if set, new users must enter this code to register (owner-controlled)
  DEFAULT_STORE  store id new counts belong to (default "valley")
  SLOTS          box layout, default "A:24,B:24" (two 24-count units = 48 boxes)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (Flask, g, redirect, render_template, request, session,
                   url_for, jsonify, abort)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from werkzeug.security import generate_password_hash, check_password_hash

from ..scans import Scan, ScanLog
from ..session import CountSession, standard_slots
from ..reporting import daily_report, render_daily_report_md
from ..config import Config
from .models import Base, User, ScanRow, ActiveCount

ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "data"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _normalize_db_url(url: str) -> str:
    # Supabase/Heroku-style "postgres://" -> SQLAlchemy's "postgresql+psycopg://"
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _parse_slots(spec: str | None) -> list:
    """Parse the SLOTS env into box labels.
      "48"          -> 1..48 (plain numeric — the current layout)
      "A:24,B:24"   -> A1..A24, B1..B24 (lettered units)
    """
    if not spec:
        return standard_slots()
    pairs = []
    for part in spec.split(","):
        part = part.strip()
        if ":" in part:
            letter, _, count = part.partition(":")
            pairs.append((letter.strip(), int(count)))
        elif part:
            pairs.append(("", int(part)))   # bare number => plain 1..N
    return standard_slots(tuple(pairs))


def _load_prices() -> dict:
    """Ticket prices per game, read from the scraper's state.json if present."""
    p = DATA_DIR / "state.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text() or "{}")
    games = raw.get("games", raw)
    out = {}
    for num, gd in games.items():
        price = gd.get("price")
        if price is not None:
            out[str(num)] = float(price)
    return out


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    cfg = config or {}

    db_url = cfg.get("DATABASE_URL") or os.environ.get("DATABASE_URL") \
        or f"sqlite:///{DATA_DIR / 'valley.db'}"
    secret = cfg.get("SECRET_KEY") or os.environ.get("SECRET_KEY")
    if not secret:
        app.logger.warning("SECRET_KEY not set — using an insecure dev key. Set it in prod!")
        secret = "dev-insecure-key-change-me"

    app.config.update(
        SECRET_KEY=secret,
        REGISTER_CODE=cfg.get("REGISTER_CODE", os.environ.get("REGISTER_CODE")),
        DEFAULT_STORE=cfg.get("DEFAULT_STORE", os.environ.get("DEFAULT_STORE", "valley")),
        SLOTS=_parse_slots(cfg.get("SLOTS", os.environ.get("SLOTS"))),
    )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    engine = create_engine(_normalize_db_url(db_url), future=True,
                           connect_args={"check_same_thread": False} if db_url.startswith("sqlite") else {})
    Base.metadata.create_all(engine)  # auto-create tables (incl. on Supabase)
    app.config["SESSION_FACTORY"] = sessionmaker(bind=engine, expire_on_commit=False)

    _register_routes(app)
    return app


def _db():
    if "db" not in g:
        g.db = g.session_factory()
    return g.db


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapped


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return _db().get(User, uid)


def _register_routes(app: Flask):

    @app.before_request
    def _open_db():
        g.session_factory = app.config["SESSION_FACTORY"]

    @app.teardown_request
    def _close_db(exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/")
    def index():
        return redirect(url_for("count" if session.get("user_id") else "login"))

    # --- registration & login --------------------------------------------
    @app.route("/register", methods=["GET", "POST"])
    def register():
        need_code = bool(app.config["REGISTER_CODE"])
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            pw = request.form.get("password") or ""
            code = request.form.get("code") or ""
            err = None
            if need_code and code != app.config["REGISTER_CODE"]:
                err = "Wrong registration code."
            elif not email or not pw:
                err = "Email and password are required."
            elif _db().scalar(select(User).where(User.email == email)):
                err = "That email is already registered."
            if err:
                return render_template("register.html", error=err, need_code=need_code, email=email)
            # First user becomes admin.
            role = "admin" if _db().scalar(select(User).limit(1)) is None else "clerk"
            user = User(email=email, password_hash=generate_password_hash(pw), role=role)
            _db().add(user)
            _db().commit()
            session["user_id"] = user.id
            session["email"] = user.email
            return redirect(url_for("count"))
        return render_template("register.html", error=None, need_code=need_code, email="")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            pw = request.form.get("password") or ""
            user = _db().scalar(select(User).where(User.email == email))
            if not user or not check_password_hash(user.password_hash, pw):
                return render_template("login.html", error="Invalid email or password.", email=email)
            session["user_id"] = user.id
            session["email"] = user.email
            return redirect(request.args.get("next") or url_for("count"))
        return render_template("login.html", error=None, email="")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # --- active count session helpers ------------------------------------
    def _store():
        return app.config["DEFAULT_STORE"]

    def _active_row():
        return _db().scalar(select(ActiveCount).where(
            ActiveCount.store == _store(),
            ActiveCount.user_email == session.get("email")))

    def _load_session() -> CountSession | None:
        row = _active_row()
        return CountSession.from_state(json.loads(row.state_json)) if row else None

    def _save_session(cs: CountSession):
        row = _active_row()
        payload = json.dumps(cs.to_state())
        if row:
            row.state_json = payload
            row.updated_at = datetime.now(timezone.utc)
        else:
            _db().add(ActiveCount(store=_store(), user_email=session.get("email"),
                                  state_json=payload))
        _db().commit()

    def _clear_session():
        row = _active_row()
        if row:
            _db().delete(row)
            _db().commit()

    def _state_payload(cs: CountSession, step=None):
        done, total = cs.progress()
        return {
            "session": cs.session, "store": cs.store,
            "current_slot": cs.current_slot,
            "done": done, "total": total,
            "complete": cs.is_complete(),
            "pending": cs.pending(),
            "slots": [
                {"slot": s,
                 "game": cs.entries[s].game_number if s in cs.entries else None,
                 "ticket": cs.entries[s].ticket if s in cs.entries else None,
                 "scanned": s in cs.entries}
                for s in cs.slots
            ],
            "step": step.to_dict() if step else None,
        }

    # --- scan page & API --------------------------------------------------
    @app.get("/count")
    @login_required
    def count():
        cs = _load_session()
        return render_template("count.html", has_active=cs is not None,
                               state=(_state_payload(cs) if cs else None),
                               email=session.get("email"))

    @app.post("/count/start")
    @login_required
    def count_start():
        body = request.get_json(silent=True) or {}
        label = request.form.get("session") or body.get("session") or "morning"
        cs = CountSession(slots=app.config["SLOTS"], store=_store(),
                          session=label, user=session.get("email"))
        _save_session(cs)
        return jsonify(_state_payload(cs))

    def _require_session() -> CountSession:
        cs = _load_session()
        if cs is None:
            abort(409, "no active count — start one first")
        return cs

    @app.get("/api/state")
    @login_required
    def api_state():
        cs = _load_session()
        return jsonify(_state_payload(cs) if cs else {"current_slot": None, "complete": False, "slots": []})

    @app.post("/api/scan")
    @login_required
    def api_scan():
        cs = _require_session()
        raw = (request.get_json(silent=True) or {}).get("raw", "")
        step = cs.scan(raw, at=_now_iso())
        _save_session(cs)
        return jsonify(_state_payload(cs, step))

    @app.post("/api/rescan")
    @login_required
    def api_rescan():
        cs = _require_session()
        body = request.get_json(silent=True) or {}
        step = cs.rescan(body.get("slot", ""), body.get("raw", ""), at=_now_iso())
        _save_session(cs)
        return jsonify(_state_payload(cs, step))

    @app.post("/api/back")
    @login_required
    def api_back():
        cs = _require_session()
        step = cs.back()
        _save_session(cs)
        return jsonify(_state_payload(cs, step))

    @app.post("/api/skip")
    @login_required
    def api_skip():
        cs = _require_session()
        step = cs.skip()
        _save_session(cs)
        return jsonify(_state_payload(cs, step))

    @app.post("/api/goto")
    @login_required
    def api_goto():
        cs = _require_session()
        step = cs.goto((request.get_json(silent=True) or {}).get("slot", ""))
        _save_session(cs)
        return jsonify(_state_payload(cs, step))

    @app.post("/api/commit")
    @login_required
    def api_commit():
        cs = _require_session()
        scans = cs.finalize()
        for sc in scans:
            _db().add(ScanRow(store=sc.store, game_number=sc.game_number, pack=sc.pack,
                              ticket=sc.ticket, slot=sc.slot, session=sc.session,
                              scanned_at=sc.scanned_at, user_email=sc.user, raw=sc.raw))
        _db().commit()
        _clear_session()
        return jsonify({"committed": len(scans), "date": _today()})

    # --- report -----------------------------------------------------------
    @app.get("/report")
    @login_required
    def report():
        date = request.args.get("date") or _today()
        rows = _db().scalars(select(ScanRow).where(ScanRow.store == _store())).all()
        log = ScanLog(scans=[r.to_scan() for r in rows])
        try:
            resolver = Config.load(str(ROOT / "config.yaml")).pack_resolver()
        except Exception:  # noqa: BLE001 — report must render even without config
            resolver = None
        rep = daily_report(log, date, prices=_load_prices(), resolver=resolver, store=_store())
        return render_template("report.html", date=date, report=rep,
                               md=render_daily_report_md(rep), email=session.get("email"))


def main():
    app = create_app()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("FLASK_DEBUG")))


if __name__ == "__main__":
    main()
