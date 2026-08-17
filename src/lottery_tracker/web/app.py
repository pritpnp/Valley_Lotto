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
import re
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import (Flask, current_app, g, redirect, render_template, request,
                   session, url_for, jsonify, abort)
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from werkzeug.security import generate_password_hash, check_password_hash

from ..barcode import try_parse_ticket
from ..scans import Scan, ScanLog
from ..session import CountSession, standard_slots
from ..reporting import daily_report, render_daily_report_md
from ..config import Config
from ..rules import RATING_FACTORS
from .models import (Base, User, ScanRow, ActiveCount, InventoryRow, EmphasisRow,
                     StaffRow, AccessRow)

# The KEEP/SEND-BACK engine. pa_data is framework-neutral (it imports only from
# lottery_tracker), so the dashboard math is shared with the FastAPI app and the
# static GitHub Pages report rather than reimplemented here.
from lottery_app import pa_data

ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "data"

# Slider copy for the rating factors (same wording as the FastAPI dashboard).
FACTOR_LABELS = {
    "odds": ("Win odds", "Chance to win ANY prize (break-even shot)"),
    "prizes_left": ("Prizes left", "How much of the whole game is still unsold"),
    "low_prize": ("Low-prize stock", "Cheap, commonly-won prizes still in the pack"),
    "low_prize_skew": ("Low-prize trend", "Penalize when cheap prizes drain faster than the rest"),
    "jackpot_density": ("Jackpot density", "Big prizes still available (for jackpot chasers)"),
}


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


def _env(name: str, default: str | None = None) -> str | None:
    """Read an env var, tolerating quotes.

    Some hosting dashboards (Railway's raw editor among them) may pass values
    through with the surrounding quotes intact, so SLOTS="48" can arrive as the
    literal 6-character string. Strip them rather than crashing on boot.
    """
    val = os.environ.get(name, default)
    if isinstance(val, str):
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1].strip()
    return val


def _as_bool(val) -> bool:
    """Env values arrive as strings: '1', 'true', 'yes', 'on' all mean True."""
    if isinstance(val, bool):
        return val
    return str(val or "").strip().strip("\"'").lower() in {"1", "true", "yes", "on"}


def _parse_slots(spec: str | None) -> list:
    """Parse the SLOTS setting into box labels.
      "48"          -> 1..48 (plain numeric — the current layout)
      "A:24,B:24"   -> A1..A24, B1..B24 (lettered units)
    A malformed value falls back to the default layout instead of taking the
    app down — a typo in a dashboard shouldn't cost you the whole site.
    """
    if not spec:
        return standard_slots()
    if isinstance(spec, str):
        spec = spec.strip().strip("\"'").strip()
    pairs = []
    try:
        for part in str(spec).split(","):
            part = part.strip().strip("\"'").strip()
            if not part:
                continue
            if ":" in part:
                letter, _, count = part.partition(":")
                pairs.append((letter.strip(), int(count.strip())))
            else:
                pairs.append(("", int(part)))   # bare number => plain 1..N
    except (TypeError, ValueError):
        return standard_slots()
    return standard_slots(tuple(pairs)) if pairs else standard_slots()


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

    db_url = cfg.get("DATABASE_URL") or _env("DATABASE_URL") \
        or f"sqlite:///{DATA_DIR / 'valley.db'}"
    if db_url.startswith("sqlite"):
        # Loud, because on an ephemeral host (Railway/Render) this means every
        # redeploy silently wipes accounts, inventory, and scans.
        app.logger.warning(
            "DATABASE_URL is not set — falling back to local SQLite at %s. "
            "On a hosted platform this data is LOST on every redeploy; set "
            "DATABASE_URL to your Postgres/Supabase connection string.", db_url)

    secret = cfg.get("SECRET_KEY") or _env("SECRET_KEY")
    if not secret:
        app.logger.warning("SECRET_KEY not set — using an insecure dev key. Set it in prod!")
        secret = "dev-insecure-key-change-me"

    app.config.update(
        SECRET_KEY=secret,
        REGISTER_CODE=cfg.get("REGISTER_CODE", _env("REGISTER_CODE")),
        DEFAULT_STORE=cfg.get("DEFAULT_STORE", _env("DEFAULT_STORE", "valley")),
        SLOTS=_parse_slots(cfg.get("SLOTS", _env("SLOTS"))),
        # PIN_ONLY drops the store email/password from the front end: the site
        # opens straight to the PIN pad. Convenient on a counter device, but it
        # means anyone with the URL only has a short PIN in their way — keep the
        # URL private, and turn this off to restore the password gate.
        PIN_ONLY=_as_bool(cfg.get("PIN_ONLY", _env("PIN_ONLY"))),
    )

    app.permanent_session_lifetime = timedelta(days=90)   # keep the device signed in
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
def client_ip() -> str:
    """The real client address.

    Behind Railway (and any proxy) ``remote_addr`` is the proxy, so prefer the
    left-most entry of X-Forwarded-For, which is the original client. That header
    is client-supplied and therefore spoofable — it's good enough to spot an
    unfamiliar device, not to prove identity.
    """
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.headers.get("X-Real-IP") or request.remote_addr or "")[:64]


def log_access(event: str = "page", status: int = 0) -> None:
    """Record one request. Never let logging break the request it's logging."""
    try:
        _db().add(AccessRow(
            store=getattr(g, "store", "default"), ip=client_ip(),
            method=request.method, path=request.path[:255], status=status,
            event=event, staff_name=session.get("staff_name"),
            user_agent=(request.headers.get("User-Agent") or "")[:255],
        ))
        _db().commit()
    except Exception:  # noqa: BLE001
        try:
            _db().rollback()
        except Exception:  # noqa: BLE001
            pass


def _pin_only() -> bool:
    return bool(current_app.config.get("PIN_ONLY"))


def _store_has_staff() -> bool:
    return _db().scalar(select(StaffRow).where(
        StaffRow.store == g.store, StaffRow.active.is_(True)).limit(1)) is not None


def _signed_in() -> bool:
    """Is this request allowed past the front door?

    Password mode: the store account must be signed in.
    PIN-only mode: a person must be unlocked — unless no staff exist yet, which
    would otherwise lock a fresh install out of the page used to create them.
    """
    if _pin_only():
        return bool(session.get("staff_id")) or not _store_has_staff()
    return bool(session.get("user_id"))


def login_required(view):
    """The front door. Store password, or a PIN when PIN_ONLY is set."""
    @wraps(view)
    def wrapped(*a, **kw):
        if _signed_in():
            return view(*a, **kw)
        dest = "pin" if _pin_only() else "login"
        return redirect(url_for(dest, next=request.path))
    return wrapped


def staff_required(view):
    """Layer 2: a person must be unlocked by PIN.

    The store login stays put; this only asks *who* is working. Pages that
    record who did something (scanning) use this; read-only pages don't need to.
    If the store has no staff configured yet, this is a no-op so a brand-new
    install isn't locked out of its own scanner.
    """
    @wraps(view)
    def wrapped(*a, **kw):
        if not _signed_in():
            dest = "pin" if _pin_only() else "login"
            return redirect(url_for(dest, next=request.path))
        if session.get("staff_id") or not _store_has_staff():
            return view(*a, **kw)          # unlocked, or no PINs set up yet
        return redirect(url_for("pin", next=request.path))
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
        g.store = app.config["DEFAULT_STORE"]
        # The store login is meant to persist on a counter device for weeks, so
        # the clerk never types a password; the PIN layer identifies the person.
        session.permanent = True

    @app.context_processor
    def _inject_identity():
        """Every page shows who's signed in (store) and who's working (person),
        so routes don't each have to pass them."""
        try:
            signed_in = _signed_in()
        except Exception:  # noqa: BLE001 — never let the chrome break a page
            signed_in = bool(session.get("user_id") or session.get("staff_id"))
        return {"email": session.get("email"),
                "staff_name": session.get("staff_name"),
                "pin_only": bool(app.config.get("PIN_ONLY")),
                "signed_in": signed_in}

    # Skip the noise: Railway health checks and static assets would otherwise
    # dominate the log and hide the traffic you actually want to see.
    _NO_LOG = {"/healthz", "/favicon.ico"}

    @app.after_request
    def _log_request(resp):
        if request.path not in _NO_LOG and not request.path.startswith("/static"):
            # PIN attempts log themselves with a precise outcome; don't double-log.
            if not (request.path == "/pin" and request.method == "POST"):
                log_access("page", resp.status_code)
        return resp

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
        if _signed_in():
            return redirect(url_for("count"))
        return redirect(url_for("pin" if _pin_only() else "login"))

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

    @app.get("/logout")
    def logout_confirm():
        """Ask before signing out.

        Password mode: this drops the STORE session, which needs the owner's
        password to restore — worth confirming. PIN-only mode: there is no store
        password, so this just ends the current person's shift.
        """
        if not _signed_in():
            return redirect(url_for("pin" if _pin_only() else "login"))
        return render_template("logout.html")

    @app.post("/logout")
    def logout():
        if _pin_only():
            # Only end the shift; there's no store credential to give up.
            session.pop("staff_id", None)
            session.pop("staff_name", None)
            return redirect(url_for("pin"))
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
        if not row:
            return None
        cs = CountSession.from_state(json.loads(row.state_json))
        cs.known_games = _known_games()   # refreshed per request, never serialized
        return cs

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
    @staff_required
    def count():
        cs = _load_session()
        return render_template("count.html", has_active=cs is not None,
                               state=(_state_payload(cs) if cs else None),
                               email=session.get("email"))

    @app.post("/count/start")
    @staff_required
    def count_start():
        body = request.get_json(silent=True) or {}
        label = request.form.get("session") or body.get("session") or "morning"
        cs = CountSession(slots=app.config["SLOTS"], store=_store(),
                          session=label, user=session.get("staff_name") or session.get("email"),
                          known_games=_known_games())
        _save_session(cs)
        return jsonify(_state_payload(cs))

    def _require_session() -> CountSession:
        cs = _load_session()
        if cs is None:
            abort(409, "no active count — start one first")
        return cs

    @app.get("/api/state")
    @staff_required
    def api_state():
        cs = _load_session()
        return jsonify(_state_payload(cs) if cs else {"current_slot": None, "complete": False, "slots": []})

    @app.post("/api/scan")
    @staff_required
    def api_scan():
        cs = _require_session()
        raw = (request.get_json(silent=True) or {}).get("raw", "")
        step = cs.scan(raw, at=_now_iso())
        _save_session(cs)
        return jsonify(_state_payload(cs, step))

    @app.post("/api/rescan")
    @staff_required
    def api_rescan():
        cs = _require_session()
        body = request.get_json(silent=True) or {}
        step = cs.rescan(body.get("slot", ""), body.get("raw", ""), at=_now_iso())
        _save_session(cs)
        return jsonify(_state_payload(cs, step))

    @app.post("/api/back")
    @staff_required
    def api_back():
        cs = _require_session()
        step = cs.back()
        _save_session(cs)
        return jsonify(_state_payload(cs, step))

    @app.post("/api/skip")
    @staff_required
    def api_skip():
        cs = _require_session()
        step = cs.skip()
        _save_session(cs)
        return jsonify(_state_payload(cs, step))

    @app.post("/api/goto")
    @staff_required
    def api_goto():
        cs = _require_session()
        step = cs.goto((request.get_json(silent=True) or {}).get("slot", ""))
        _save_session(cs)
        return jsonify(_state_payload(cs, step))

    @app.post("/api/commit")
    @staff_required
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

    # --- who is working: PIN layer ----------------------------------------
    def _staff_list(active_only: bool = True):
        q = select(StaffRow).where(StaffRow.store == _store())
        if active_only:
            q = q.where(StaffRow.active.is_(True))
        return _db().scalars(q.order_by(StaffRow.name)).all()

    @app.route("/pin", methods=["GET", "POST"])
    def pin():
        """Unlock a shift with a PIN.

        This is deliberately NOT behind ``login_required``: in PIN-only mode it
        *is* the front door, and guarding it would redirect the page to itself.
        In password mode it still requires the store session, since it's only the
        second layer there.
        """
        if not _pin_only() and not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        staff = _staff_list()
        nxt = request.args.get("next") or request.form.get("next") or url_for("count")
        if request.method == "POST":
            entered = (request.form.get("pin") or "").strip()
            who = request.form.get("staff_id")
            # Match the PIN against the named person, or any person if none picked.
            candidates = [s for s in staff if str(s.id) == str(who)] if who else staff
            for member in candidates:
                if entered and check_password_hash(member.pin_hash, entered):
                    session["staff_id"] = member.id
                    session["staff_name"] = member.name
                    log_access("pin_ok", 302)
                    return redirect(nxt)
            log_access("pin_fail", 200)   # the row that matters if someone probes
            return render_template("pin.html", staff=staff, error="Wrong PIN.",
                                   next=nxt, email=session.get("email"),
                                   staff_name=session.get("staff_name"))
        return render_template("pin.html", staff=staff, error=None, next=nxt,
                               email=session.get("email"),
                               staff_name=session.get("staff_name"))

    @app.post("/pin/clear")
    def pin_clear():
        """End this person's shift without signing the store out."""
        session.pop("staff_id", None)
        session.pop("staff_name", None)
        return redirect(url_for("pin"))

    @app.route("/staff", methods=["GET", "POST"])
    @login_required
    def staff_page():
        """Manage who can sign in with a PIN at this store."""
        error = None
        if request.method == "POST":
            action = request.form.get("action") or "add"
            if action == "add":
                name = (request.form.get("name") or "").strip()
                pin_val = (request.form.get("pin") or "").strip()
                if not name or not pin_val:
                    error = "Name and PIN are required."
                elif not re.fullmatch(r"\d{4,8}", pin_val):
                    error = "PIN must be 4-8 digits."
                elif _db().scalar(select(StaffRow).where(
                        StaffRow.store == _store(), StaffRow.name == name)):
                    error = f"{name} already exists."
                else:
                    _db().add(StaffRow(store=_store(), name=name,
                                       pin_hash=generate_password_hash(pin_val),
                                       role=request.form.get("role") or "clerk"))
                    _db().commit()
            elif action == "remove":
                row = _db().get(StaffRow, int(request.form.get("staff_id") or 0))
                if row and row.store == _store():
                    _db().delete(row)
                    _db().commit()
                    if session.get("staff_id") == row.id:
                        session.pop("staff_id", None)
                        session.pop("staff_name", None)
            elif action == "reset_pin":
                row = _db().get(StaffRow, int(request.form.get("staff_id") or 0))
                pin_val = (request.form.get("pin") or "").strip()
                if row and row.store == _store() and re.fullmatch(r"\d{4,8}", pin_val):
                    row.pin_hash = generate_password_hash(pin_val)
                    _db().commit()
                else:
                    error = "PIN must be 4-8 digits."
            if not error:
                return redirect(url_for("staff_page"))
        return render_template("staff.html", staff=_staff_list(active_only=False),
                               error=error, email=session.get("email"),
                               staff_name=session.get("staff_name"))

    @app.get("/access")
    @login_required
    def access_log():
        """Who has been reaching this site. Worth a glance whenever the front
        door is PIN-only, especially the failed-PIN rows."""
        rows = _db().scalars(
            select(AccessRow).where(AccessRow.store == _store())
            .order_by(AccessRow.at.desc()).limit(300)).all()

        # Roll up per IP so an unfamiliar device stands out immediately.
        by_ip: dict = {}
        for r in rows:
            e = by_ip.setdefault(r.ip or "?", {
                "ip": r.ip or "?", "hits": 0, "fails": 0, "people": set(),
                "first": r.at, "last": r.at, "agent": r.user_agent})
            e["hits"] += 1
            if r.event == "pin_fail":
                e["fails"] += 1
            if r.staff_name:
                e["people"].add(r.staff_name)
            e["first"] = min(e["first"], r.at)
            e["last"] = max(e["last"], r.at)
        summary = sorted(by_ip.values(), key=lambda e: e["last"], reverse=True)
        for e in summary:
            e["people"] = ", ".join(sorted(e["people"])) or "—"

        fails = [r for r in rows if r.event == "pin_fail"]
        return render_template("access.html", rows=rows[:120], summary=summary,
                               fail_count=len(fails))

    # --- KEEP / SEND-BACK dashboard ---------------------------------------
    # The rating math is imported (pa_data -> lottery_tracker.rules), so these
    # pages agree exactly with the twice-daily static report and never re-derive
    # the algorithm.
    def _catalog():
        return pa_data.load_catalog(DATA_DIR / "state.json")

    def _known_games() -> set:
        """Real PA game numbers, handed to the barcode parser so a scan is
        validated against games that exist instead of trusted by digit offset."""
        try:
            return set(_catalog().games.keys())
        except Exception:  # noqa: BLE001 — scanning must work even if the catalog is missing
            return set()

    def _inventory() -> set:
        rows = _db().scalars(select(InventoryRow).where(InventoryRow.store == _store())).all()
        return {r.game_number for r in rows}

    def _emphasis_row() -> EmphasisRow:
        row = _db().get(EmphasisRow, _store())
        if row is None:
            row = EmphasisRow(store=_store())
            _db().add(row)
            _db().commit()
        return row

    def _cfg() -> Config:
        try:
            return Config.load(str(ROOT / "config.yaml"))
        except Exception:  # noqa: BLE001 — a bad config must not take the app down
            return Config({})

    def _effective_weights():
        """Base weights from config.yaml, scaled by this store's sliders."""
        cfg = _cfg()
        return cfg.rating_weights.scaled(_emphasis_row().to_emphasis()), cfg

    @app.get("/dashboard")
    @login_required
    def dashboard():
        cat = _catalog()
        inv = _inventory()
        weights, cfg = _effective_weights()
        rows = pa_data.store_rows(cat, inv, cfg.thresholds, weights)
        return render_template(
            "dashboard.html", email=session.get("email"),
            rows=rows, summary=pa_data.store_summary(rows),
            captured_at=cat.captured_at, weights=weights,
            new_games=pa_data.new_games(cat, within_days=14, weights=weights),
            bring_in=pa_data.bring_in_candidates(
                cat, inv, cfg.thresholds, weights=weights,
                min_left=cfg.bring_in_min_left, per_price=cfg.bring_in_per_price),
        )

    @app.get("/catalog")
    @login_required
    def catalog():
        cat = _catalog()
        inv = _inventory()
        weights, cfg = _effective_weights()
        rows = pa_data.catalog_rankings(cat, cfg.thresholds, weights, inventory=inv)
        by_price: dict = {}
        for r in rows:
            by_price.setdefault(r["price"] or 0, []).append(r)
        by_price = dict(sorted(by_price.items(), key=lambda kv: -kv[0]))
        return render_template("catalog.html", email=session.get("email"),
                               by_price=by_price, cutoff=weights.cutoff,
                               captured_at=cat.captured_at)

    @app.get("/inventory")
    @login_required
    def inventory():
        cat = _catalog()
        inv = _inventory()
        weights, cfg = _effective_weights()
        games = cat.games
        carried = sorted((games[n] for n in inv if n in games),
                         key=lambda g: (-(g.price or 0), g.game_number))
        missing = sorted(n for n in inv if n not in games)
        available = sorted((g for n, g in games.items()
                            if g.status == "active" and n not in inv),
                           key=lambda g: (-(g.price or 0), g.game_number))
        return render_template("inventory.html", email=session.get("email"),
                               carried=carried, missing=missing, available=available)

    @app.post("/inventory/add")
    @login_required
    def inventory_add():
        """Add games by number — or by scanning a ticket.

        A clerk with the gun in hand will scan a ticket into this box, which sends
        the full barcode (e.g. 1742011331200893). Pull the game number out of it
        rather than storing the whole code as a bogus "game". Anything that is
        neither a barcode nor a plausible 3-5 digit game number is ignored, so a
        stray scan can't pollute the inventory.
        """
        raw = (request.form.get("game_number") or "").strip()
        known = _known_games()
        added = 0
        for token in re.split(r"[\s,]+", raw):
            token = token.strip()
            if not token:
                continue

            tc = try_parse_ticket(token, known)
            if tc is not None:
                num = tc.game_number              # scanned a ticket -> use its game
            elif re.fullmatch(r"\d{3,5}", token):
                num = token.lstrip("0") or token  # typed a game number
            else:
                continue                          # not a game number or a ticket

            exists = _db().scalar(select(InventoryRow).where(
                InventoryRow.store == _store(), InventoryRow.game_number == num))
            if not exists:
                _db().add(InventoryRow(store=_store(), game_number=num))
                added += 1
        if added:
            _db().commit()
        return redirect(request.form.get("next") or url_for("inventory"))

    @app.post("/inventory/remove")
    @login_required
    def inventory_remove():
        num = (request.form.get("game_number") or "").strip()
        row = _db().scalar(select(InventoryRow).where(
            InventoryRow.store == _store(), InventoryRow.game_number == num))
        if row:
            _db().delete(row)
            _db().commit()
        return redirect(request.form.get("next") or url_for("inventory"))

    @app.route("/weights", methods=["GET", "POST"])
    @login_required
    def weights_page():
        row = _emphasis_row()
        if request.method == "POST":
            for f in RATING_FACTORS:
                try:
                    v = float(request.form.get(f, 0.0) or 0.0)
                except ValueError:
                    v = 0.0
                setattr(row, f, max(-3.0, min(3.0, v)))   # clamp to the slider range
            _db().commit()
            return redirect(url_for("weights_page"))

        cfg = _cfg()
        base = cfg.rating_weights
        effective = base.scaled(row.to_emphasis())
        total = sum(getattr(effective, f) for f in RATING_FACTORS) or 1.0
        sliders = [{
            "key": f, "label": FACTOR_LABELS[f][0], "desc": FACTOR_LABELS[f][1],
            "value": getattr(row, f), "base": getattr(base, f),
            "eff_pct": 100 * getattr(effective, f) / total,
        } for f in RATING_FACTORS]
        return render_template("weights.html", email=session.get("email"),
                               sliders=sliders, cutoff=effective.cutoff)

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
