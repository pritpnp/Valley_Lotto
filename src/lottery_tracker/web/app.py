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
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash

from ..barcode import try_parse_ticket, parse_ticket, BarcodeError
from ..scans import Scan, ScanLog
from ..session import CountSession, standard_slots
from ..reporting import (daily_report, render_daily_report_md, as_zone,
                         business_date, count_status, normalize_session,
                         SESSIONS)
from ..config import Config
from ..rules import RATING_FACTORS, rate, recommendation
from .models import (Base, User, Store, ScanRow, ActiveCount, InventoryRow,
                     PackRow, ShipmentRow,
                     EmphasisRow, StaffRow, AccessRow, AuditRow, BoxRow)
from .migrate import ensure_schema
from . import permissions as perms

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


def _today(tz=None) -> str:
    """Today's date in the STORE's timezone, not UTC."""
    zone = as_zone(tz)
    now = datetime.now(timezone.utc)
    if zone is not None:
        now = now.astimezone(zone)
    return now.strftime("%Y-%m-%d")


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


def _logo_file() -> str | None:
    """The brand logo to display, if one has been supplied.

    Kept as a file lookup rather than a hardcoded name so the logo can be
    swapped by dropping an image in, with no code change.
    """
    static = Path(__file__).resolve().parent / "static"
    if not static.exists():
        return None
    exts = (".svg", ".png", ".webp", ".jpg", ".jpeg")
    # An exact logo.* wins, but any image with "logo" in the name is accepted too,
    # so a file can be uploaded under its own name without being renamed first.
    for name in ("logo" + e for e in exts):
        if (static / name).exists():
            return name
    named = sorted(p.name for p in static.iterdir()
                   if p.suffix.lower() in exts and "logo" in p.stem.lower()
                   and "favicon" not in p.stem.lower())
    return named[0] if named else None


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
        # The store's own timezone. Scans are stored in UTC, but a "day" —
        # morning count to night count — is local. Without this a 9pm PA count
        # (1am UTC) files under tomorrow and splits the day in two.
        TIMEZONE=cfg.get("TIMEZONE", _env("TIMEZONE", "America/New_York")),
    )

    app.permanent_session_lifetime = timedelta(days=90)   # keep the device signed in
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    engine = create_engine(_normalize_db_url(db_url), future=True,
                           connect_args={"check_same_thread": False} if db_url.startswith("sqlite") else {})
    # Additive migration + backfill; safe on every boot and on a live database
    # that predates multi-store. See migrate.py.
    ensure_schema(engine,
                  default_slug=app.config["DEFAULT_STORE"],
                  default_name=cfg.get("DEFAULT_STORE_NAME",
                                       _env("DEFAULT_STORE_NAME", "Valley Lotto")),
                  timezone=app.config["TIMEZONE"],
                  # The store row must inherit the configured layout, not a
                  # hardcoded default — otherwise a store created on first boot
                  # silently gets 48 boxes regardless of SLOTS.
                  slots=str(cfg.get("SLOTS") or _env("SLOTS") or "48"))
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


def current_user():
    uid = session.get("user_id")
    return _db().get(User, uid) if uid else None


def acting_store_slug() -> str | None:
    """Which store this request is operating on.

    A manager is pinned to their own store. A superadmin belongs to none, so they
    pick one ("act as"), which is remembered in the session.
    """
    if session.get("role") == "superadmin":
        return session.get("acting_store")
    return session.get("user_store")


def current_store():
    slug = acting_store_slug()
    return _db().get(Store, slug) if slug else None


def effective_role() -> str:
    """What the person in front of the screen may do.

    Subtle but important: the counter device stays signed in as the MANAGER, so
    the session's own role is too generous once an employee unlocks a shift with
    their PIN. While someone is PIN'd in, their staff role wins — otherwise an
    employee would inherit the manager's ability to add staff or change settings.
    """
    staff_role = session.get("staff_role")
    if staff_role:
        return "employee" if staff_role in ("clerk", "employee") else staff_role
    role = session.get("role")
    if role:
        return role
    # PIN-only single-store mode with nobody PIN'd in. If no PINs exist yet this
    # is the bootstrap window (someone has to be able to create the first one),
    # so grant manager — enough to set up a store, never chain administration.
    if _pin_only():
        try:
            return "employee" if _store_has_staff() else "manager"
        except Exception:  # noqa: BLE001
            return "employee"
    return "employee"


def _is_at_least(role: str, needed: str) -> bool:
    order = {"employee": 0, "manager": 1, "superadmin": 2}
    return order.get(role, 0) >= order.get(needed, 0)


def granted_keys() -> set:
    """Every capability the person at the screen holds.

    A manager or superadmin holds all of them by virtue of the role. An employee
    holds exactly what was ticked next to their name, which is the whole point:
    the person who receives deliveries can be given that job and nothing else.
    """
    if _is_at_least(effective_role(), "manager"):
        return set(perms.ALL_KEYS)
    return perms.parse(session.get("staff_perms"))


def can(key: str) -> bool:
    return key in granted_keys()


def perm_required(key: str):
    """Gate a page on one capability rather than on rank."""
    def deco(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if not _signed_in():
                dest = "pin" if _pin_only() else "login"
                return redirect(url_for(dest, next=request.path))
            if not can(key):
                abort(403)
            return view(*a, **kw)
        return wrapped
    return deco


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


def role_required(minimum: str):
    """Gate a page on the effective role (employee < manager < superadmin)."""
    def deco(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if not _signed_in():
                dest = "pin" if _pin_only() else "login"
                return redirect(url_for(dest, next=request.path))
            if not _is_at_least(effective_role(), minimum):
                abort(403)
            return view(*a, **kw)
        return wrapped
    return deco


manager_required = role_required("manager")
superadmin_required = role_required("superadmin")


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
        store = None
        try:
            store = current_store()
        except Exception:  # noqa: BLE001
            pass
        role = effective_role() if session.get("user_id") else None
        return {
            "email": session.get("username") or session.get("email"),
            "username": session.get("username"),
            "staff_name": session.get("staff_name"),
            "pin_only": bool(app.config.get("PIN_ONLY")),
            "signed_in": signed_in,
            "role": role,
            "is_superadmin": session.get("role") == "superadmin",
            "is_manager": _is_at_least(role or "employee", "manager"),
            # Menus and buttons ask `can.reports` etc. rather than guessing from
            # the role, so a part-manager sees exactly what they can actually do.
            "can": {k: (k in granted_keys()) for k in perms.ALL_KEYS},
            "store": store,
            # Always the store being operated on — a clerk (and you, when acting
            # as a store) can tell at a glance which location this screen is for.
            "site_title": store.title if store else "Valley Lotto",
            # Drop a real logo at web/static/logo.(png|svg|jpg) and it is used
            # verbatim; until then the header falls back to the drawn mark.
            "logo_file": _logo_file(),
            "all_stores": (_db().scalars(select(Store).where(Store.active.is_(True))
                                         .order_by(Store.name)).all()
                           if session.get("role") == "superadmin" else []),
        }

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
        """Bootstrap the very first account (the superadmin).

        In a chain, accounts are handed out — a manager is created by the
        superadmin, an employee by their manager. So this page is only open while
        no account exists yet, unless a REGISTER_CODE is configured.
        """
        have_users = _db().scalar(select(User).limit(1)) is not None
        need_code = bool(app.config["REGISTER_CODE"])
        if have_users and not need_code:
            return redirect(url_for("login"))

        if request.method == "POST":
            uname = (request.form.get("username") or request.form.get("email") or "").strip().lower()
            pw = request.form.get("password") or ""
            code = request.form.get("code") or ""
            err = None
            if need_code and code != app.config["REGISTER_CODE"]:
                err = "Wrong registration code."
            elif not uname or not pw:
                err = "Username and password are required."
            elif _db().scalar(select(User).where(User.username == uname)):
                err = "That username is already taken."
            if err:
                return render_template("register.html", error=err, need_code=need_code,
                                       username=uname)
            user = User(username=uname,
                        password_hash=generate_password_hash(pw),
                        # The first account runs the whole chain; later ones are
                        # ordinary managers of the default store.
                        role="superadmin" if not have_users else "manager",
                        store=None if not have_users else app.config["DEFAULT_STORE"])
            _db().add(user)
            _db().commit()
            _start_session(user)
            audit("user.register", uname)
            return redirect(url_for("dashboard"))
        return render_template("register.html", error=None, need_code=need_code, username="")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        """Sign in with a username. Managers and the superadmin have accounts;
        employees never do — they unlock a shift with a PIN on a device a manager
        already signed in."""
        if request.method == "POST":
            uname = (request.form.get("username") or "").strip()
            pw = request.form.get("password") or ""
            # Case-insensitive on the way in, so "Prit" and "prit" both work,
            # while the stored spelling stays exactly as it was typed.
            user = _db().scalar(select(User).where(
                func.lower(User.username) == uname.lower()))
            if user is None:      # legacy accounts created with an email
                user = _db().scalar(select(User).where(
                    func.lower(User.email) == uname.lower()))
            if not user or not check_password_hash(user.password_hash, pw) \
                    or user.active is False:
                log_access("login_fail", 200)
                return render_template("login.html", error="Invalid username or password.",
                                       username=uname)
            _start_session(user)
            log_access("login_ok", 302)
            return redirect(request.args.get("next") or url_for("dashboard"))
        return render_template("login.html", error=None, username="")

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
            session.pop("staff_role", None)
            session.pop("staff_perms", None)
            return redirect(url_for("pin"))
        session.clear()
        return redirect(url_for("login"))

    def _start_session(user: User) -> None:
        """Put the signed-in account into the session, including which store it
        operates on. A superadmin has no home store, so they start by acting on
        the first one and can switch."""
        session.clear()
        session.permanent = True
        session["user_id"] = user.id
        session["username"] = user.username or user.email
        session["role"] = user.role
        session["user_store"] = user.store
        if user.role == "superadmin":
            first = _db().scalars(select(Store).where(Store.active.is_(True))
                                  .order_by(Store.name)).first()
            session["acting_store"] = first.slug if first else None

    # --- active count session helpers ------------------------------------
    def _store():
        """The store this request reads and writes. Falls back to the configured
        default so a single-store install keeps working unchanged."""
        return acting_store_slug() or app.config["DEFAULT_STORE"]

    def _store_row():
        return _db().get(Store, _store())

    def _store_tz():
        row = _store_row()
        return row.timezone if row else app.config["TIMEZONE"]

    def _store_slots():
        row = _store_row()
        return _parse_slots(row.slots) if row else app.config["SLOTS"]

    def audit(action: str, detail: str = "") -> None:
        """Record a business action for the audit trail."""
        try:
            _db().add(AuditRow(store=_store(), action=action, detail=detail[:2000],
                               actor=(session.get("staff_name") or session.get("username") or "?"),
                               actor_role=effective_role(), ip=client_ip()))
            _db().commit()
        except Exception:  # noqa: BLE001 — auditing must not break the action
            _db().rollback()

    def _counter_key() -> str:
        """Who owns the in-progress count.

        Keyed to the PERSON, not the store account: in PIN-only mode there is no
        per-device email, so keying by email put every clerk on one shared row —
        two people counting at once would overwrite each other's walk.
        """
        return (session.get("staff_name") or session.get("email") or "default")[:255]

    def _active_row(lock: bool = False):
        q = select(ActiveCount).where(
            ActiveCount.store == _store(),
            ActiveCount.user_email == _counter_key())
        # Two scans arriving at once used to read the same half-finished count
        # and write over each other. On Postgres the row lock makes the second
        # request wait for the first, which holds even across gunicorn workers.
        # (SQLite has no row locks and one writer at a time anyway.)
        if lock and _db().bind is not None and _db().bind.dialect.name == "postgresql":
            q = q.with_for_update()
        return _db().scalar(q)

    def _load_session(lock: bool = False) -> CountSession | None:
        row = _active_row(lock)
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
            _db().add(ActiveCount(store=_store(), user_email=_counter_key(),
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
            # walk_done — not "every box filled" — is what ends a count, since
            # empty boxes are skipped rather than scanned.
            "walk_done": cs.walk_done,
            "pending": cs.pending_slots(),
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
        tz = _store_tz()
        today = _today(tz)
        # Show which of today's counts are already in, so the clerk picking a
        # session can see at a glance that the required night count is still owed.
        status = count_status(_store_log(), today, store=_store(), tz=tz)
        return render_template("count.html", has_active=cs is not None,
                               state=(_state_payload(cs) if cs else None),
                               counts=status, today=today,
                               email=session.get("email"))

    @app.post("/count/discard")
    @staff_required
    def count_discard():
        """Throw away a half-finished walk so a different count can be started.

        Nothing is lost that was ever saved — an in-progress count only becomes
        real on commit — but it is worth an audit line, because it's how a count
        started under the wrong session label gets corrected.
        """
        cs = _load_session()
        if cs is not None:
            done, total = cs.progress()
            audit("count.discard", f"{cs.session} count abandoned at {done}/{total} boxes")
        _clear_session()
        return jsonify({"discarded": True})

    @app.post("/count/start")
    @staff_required
    def count_start():
        body = request.get_json(silent=True) or {}
        # "evening" is the old name for the night count; fold aliases onto the
        # three real sessions so a day's counts always line up in the report.
        label = normalize_session(
            request.form.get("session") or body.get("session") or "night") or "night"
        cs = CountSession(slots=_store_slots(), store=_store(),
                          session=label, user=session.get("staff_name") or session.get("email"),
                          known_games=_known_games())
        _save_session(cs)
        return jsonify(_state_payload(cs))

    def _require_session() -> CountSession:
        cs = _load_session(lock=True)
        if cs is None:
            abort(409, "this count has already been saved — start a new one")
        return cs

    @app.errorhandler(Exception)
    def _api_errors_stay_json(err):
        """An /api/* failure must return JSON.

        The scan page fetches these; when Flask answered with an HTML error page
        the client's JSON parse threw and the screen went white. Now the page
        gets a message it can show in place, and a stale count tells the client
        to reload rather than leaving the clerk stuck."""
        code = getattr(err, "code", 500)
        if not request.path.startswith("/api/"):
            if isinstance(err, HTTPException):
                return err          # ordinary pages keep Flask's own error page
            raise err
        if code == 500:
            app.logger.exception("api error on %s", request.path)
        return jsonify({
            "error": getattr(err, "description", None) or "something went wrong — scan again",
            "restart": code == 409,
        }), code

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
        cs = _load_session()
        if cs is None or cs.committed:
            # Already saved — a second save request is a double-tap or a retry,
            # not a failure. Answer as if this one did the work.
            _clear_session()
            return jsonify({"committed": 0, "already_saved": True,
                            "date": _today(_store_tz())})
        scans = cs.finalize()
        for sc in scans:
            _db().add(ScanRow(store=sc.store, game_number=sc.game_number, pack=sc.pack,
                              ticket=sc.ticket, slot=sc.slot, session=sc.session,
                              scanned_at=sc.scanned_at, user_email=sc.user, raw=sc.raw))
        _db().commit()

        # A scan is also proof that a pack is open. Recording that keeps back stock
        # honest even when nobody counts the back room.
        #
        # But "open" and "opened TODAY" are different claims, and only the second
        # one implies anything sold. A pack we watched leave back stock was opened
        # today. A pack a count simply met for the first time has been sitting in
        # that box for who knows how long — dating it today would make the report
        # believe a fresh pack was consumed, which on a store's very first count
        # invents a full pack of sales for every box at once.
        today = _today(_store_tz())
        for sc in scans:
            row = _pack_row(sc.game_number, sc.pack, create=True, source="inferred")
            if row.state == "settled":
                continue
            was_held = row.state == "backstock"
            row.state = "active"
            row.slot = sc.slot or row.slot
            if was_held and not row.opened_on:
                row.opened_on = today       # we saw it leave the back room
        _db().commit()

        # A count IS an inventory check: each scan proves which game is in which
        # box right now, so the box map maintains itself and never goes stale.
        moved = 0
        boxes = _box_map()
        for sc in scans:
            if not sc.slot:
                continue
            known = boxes.get(sc.slot)
            if known is None or known.game_number != sc.game_number:
                _set_box(sc.slot, sc.game_number, source="scan")
                moved += 1
        if moved:
            audit("box.autofill", f"{moved} box(es) updated from the count")
        _clear_session()
        return jsonify({"committed": len(scans), "date": _today(_store_tz())})

    # --- scanner check ----------------------------------------------------
    @app.get("/scan-check")
    @staff_required
    def scan_check():
        """Scan anything and see exactly what the app read, without recording it.

        Guns differ between makes and between stores, and a number that looks
        wrong on a screen is impossible to argue with unless you can see the
        characters the gun actually sent. This shows the raw string alongside the
        reading, and lists the last scans this store saved, for the same reason.
        """
        recent = _db().scalars(select(ScanRow).where(ScanRow.store == _store())
                               .order_by(ScanRow.id.desc()).limit(20)).all()
        return render_template("scan_check.html", recent=recent)

    @app.post("/api/scan-check")
    @staff_required
    def api_scan_check():
        """Parse a scan and report it. Deliberately saves nothing."""
        raw = (request.get_json(silent=True) or {}).get("raw", "")
        digits = re.sub(r"\D", "", raw)
        out = {"ok": True, "raw": raw, "digits": digits, "length": len(digits)}
        try:
            tc = parse_ticket(raw, _known_games())
        except BarcodeError as e:
            out.update(read=False, message=f"not read as a ticket: {e}")
            return jsonify(out)
        cat = _catalog()
        g = cat.games.get(tc.game_number)
        out.update(read=True, game=tc.game_number, pack=tc.pack, ticket=tc.ticket,
                   extra=tc.extra, known=bool(g), name=(g.name if g else ""),
                   message=(f"game {tc.game_number}, pack {tc.pack}, ticket "
                            f"{tc.ticket:03d}"))
        return jsonify(out)

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
                    session["staff_role"] = member.role or "employee"
                    session["staff_perms"] = perms.dump(member.granted())
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
        session.pop("staff_role", None)
        session.pop("staff_perms", None)
        return redirect(url_for("pin"))

    @app.route("/staff", methods=["GET", "POST"])
    @perm_required("staff")
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
                    role = request.form.get("role") or "employee"
                    granted = perms.dump(request.form.getlist("perm"))
                    _db().add(StaffRow(store=_store(), name=name,
                                       pin_hash=generate_password_hash(pin_val),
                                       role=role, permissions=granted))
                    _db().commit()
                    audit("staff.add", f"{name} ({role})"
                          + (f" — {perms.describe(granted)}" if role != "manager" else ""))
            elif action == "remove":
                row = _db().get(StaffRow, int(request.form.get("staff_id") or 0))
                if row and row.store == _store():
                    _db().delete(row)
                    _db().commit()
                    if session.get("staff_id") == row.id:
                        session.pop("staff_id", None)
                        session.pop("staff_name", None)
            elif action == "permissions":
                row = _db().get(StaffRow, int(request.form.get("staff_id") or 0))
                if row and row.store == _store():
                    row.role = request.form.get("role") or "employee"
                    row.permissions = perms.dump(request.form.getlist("perm"))
                    _db().commit()
                    audit("staff.permissions",
                          f"{row.name}: {'manager — everything' if row.role == 'manager' else perms.describe(row.permissions)}")
                    # Someone changing their OWN grants mid-shift must not keep
                    # the old ones until they next sign in.
                    if session.get("staff_id") == row.id:
                        session["staff_role"] = row.role
                        session["staff_perms"] = perms.dump(row.granted())
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
                               capabilities=perms.GRANTABLE, perms=perms,
                               error=error, email=session.get("email"),
                               staff_name=session.get("staff_name"))

    # Why a pack left the floor. Free text alone would make these unusable in
    # aggregate, so the reason is a short list plus an optional note.
    SETTLE_REASONS = [
        ("send_back", "Rated send-back — poor odds or prizes gone"),
        ("slow", "Too slow — not selling here"),
        ("ended", "Game ended or PA recalled it"),
        ("damaged", "Damaged or unsellable"),
        ("space", "Making room for something better"),
        ("other", "Other"),
    ]

    # --- backstock: the unopened packs in the back room --------------------
    #
    # Box scans answer "what is on the counter". They cannot answer "what do we
    # still have", and they miss any pack that is opened and finished between two
    # counts. Following the pack itself — received, held, opened, settled — is
    # what makes both answerable.

    def _packs(*, state=None, game=None):
        q = select(PackRow).where(PackRow.store == _store())
        if state:
            q = q.where(PackRow.state == state)
        if game:
            q = q.where(PackRow.game_number == game)
        return _db().scalars(q.order_by(PackRow.game_number, PackRow.pack)).all()

    def _pack_row(game: str, pack: str, *, create: bool = False, source: str = "scan"):
        row = _db().scalar(select(PackRow).where(
            PackRow.store == _store(), PackRow.game_number == game,
            PackRow.pack == pack))
        if row is None and create:
            row = PackRow(store=_store(), game_number=game, pack=pack, source=source)
            _db().add(row)
        return row

    def _held_by_game() -> dict:
        """game_number -> [PackRow] still unopened in the back room."""
        out: dict = {}
        for row in _packs(state="backstock"):
            out.setdefault(row.game_number, []).append(row)
        return out

    def _packs_opened_on(date: str) -> dict:
        """game_number -> how many packs were opened that day.

        This is what rescues a day where a box burned through more packs than the
        counts could see.
        """
        rows = _db().scalars(select(PackRow).where(
            PackRow.store == _store(), PackRow.opened_on == date)).all()
        out: dict = {}
        for r in rows:
            out[r.game_number] = out.get(r.game_number, 0) + 1
        return out

    def _backstock_view() -> dict:
        """Everything the backstock page needs: what's held, what's short, what
        should go back."""
        held = _held_by_game()
        boxes = _box_map()
        on_floor: dict = {}
        for b in boxes.values():
            if b.game_number:
                on_floor[b.game_number] = on_floor.get(b.game_number, 0) + 1

        cat = _catalog()
        weights, cfg = _effective_weights()
        rated = {r["game_number"]: r for r in
                 pa_data.store_rows(cat, _inventory(), cfg.thresholds, weights)}

        rows = []
        for game in sorted(set(held) | set(on_floor)):
            packs = held.get(game, [])
            info = rated.get(game) or {}
            g = cat.games.get(game)
            rows.append({
                "game": game,
                "name": (g.name if g else info.get("name") or "—"),
                "price": (g.price if g else info.get("price")),
                "held": len(packs),
                "packs": packs,
                "boxes": on_floor.get(game, 0),
                "action": info.get("action"),
                "rating_str": info.get("rating_str", "—"),
                "reason": info.get("reason", ""),
                # Short = it's out on the floor with nothing behind it. That's the
                # moment to reorder, not when the box finally runs dry.
                "short": on_floor.get(game, 0) > 0 and len(packs) == 0,
                # Holding stock of a game that's rated send-back is money sitting
                # in the back room; those packs can be returned unopened.
                "return_unopened": bool(packs) and info.get("action") == "send_back",
            })
        rows.sort(key=lambda r: (not r["return_unopened"], not r["short"], r["game"]))
        return {
            "rows": rows,
            "total_packs": sum(r["held"] for r in rows),
            "short": [r for r in rows if r["short"]],
            "send_back": [r for r in rows if r["return_unopened"]],
        }

    @app.get("/backstock")
    @perm_required("backstock")
    def backstock():
        view = _backstock_view()
        recent = _db().scalars(select(ShipmentRow).where(ShipmentRow.store == _store())
                               .order_by(ShipmentRow.received_at.desc()).limit(5)).all()
        settled = _db().scalars(select(PackRow).where(
            PackRow.store == _store(), PackRow.state == "settled")
            .order_by(PackRow.settled_at.desc()).limit(25)).all()
        return render_template("backstock.html", view=view, shipments=recent,
                               settled=settled, reasons=SETTLE_REASONS,
                               reason_text=dict(SETTLE_REASONS),
                               today=_today(_store_tz()))

    # --- receiving a shipment ---------------------------------------------
    @app.route("/backstock/receive", methods=["GET", "POST"])
    @perm_required("receive")
    def backstock_receive():
        """Log a delivery, then scan the packs in it.

        The shipping label is scanned as-is: couriers' formats differ and none of
        them are ticket barcodes, so it is stored verbatim rather than parsed.
        """
        if request.method == "POST":
            row = ShipmentRow(
                store=_store(), label=(request.form.get("label") or "").strip(),
                note=(request.form.get("note") or "").strip(),
                received_on=_today(_store_tz()),
                received_by=session.get("staff_name") or session.get("username") or "")
            _db().add(row)
            _db().commit()
            session["shipment_id"] = row.id
            audit("shipment.open", f"delivery {row.label or '(no label)'} opened")
            return redirect(url_for("backstock_receive"))

        ship = None
        if session.get("shipment_id"):
            ship = _db().get(ShipmentRow, session["shipment_id"])
            if ship is not None and ship.store != _store():
                ship = None
        packs = (_db().scalars(select(PackRow).where(
            PackRow.store == _store(), PackRow.shipment_id == ship.id)
            .order_by(PackRow.added_at)).all() if ship else [])
        return render_template("receive.html", shipment=ship, packs=packs)

    @app.post("/backstock/receive/close")
    @perm_required("receive")
    def backstock_receive_close():
        ship = _db().get(ShipmentRow, session.get("shipment_id") or 0)
        if ship is not None and ship.store == _store():
            n = _db().query(PackRow).filter(PackRow.shipment_id == ship.id).count()
            audit("shipment.close", f"delivery {ship.label or '(no label)'}: {n} pack(s)")
        session.pop("shipment_id", None)
        return redirect(url_for("backstock"))

    @app.post("/api/backstock/receive")
    @perm_required("receive")
    def api_backstock_receive():
        """Scan one pack into the open delivery."""
        ship = _db().get(ShipmentRow, session.get("shipment_id") or 0)
        if ship is None or ship.store != _store():
            abort(409, "no delivery open — start one first")
        raw = (request.get_json(silent=True) or {}).get("raw", "")
        try:
            tc = parse_ticket(raw, _known_games())
        except BarcodeError as e:
            return jsonify({"ok": False, "message": f"not a ticket barcode ({e})"})

        row = _pack_row(tc.game_number, tc.pack)
        today = _today(_store_tz())
        if row is not None and row.shipment_id == ship.id:
            return jsonify({"ok": False, "message":
                            f"pack {tc.pack} is already in this delivery"})
        if row is not None and row.state == "settled":
            return jsonify({"ok": False, "message":
                            f"pack {tc.pack} was settled and returned on {row.settled_on}"})
        if row is None:
            row = _pack_row(tc.game_number, tc.pack, create=True)
        row.state = "backstock"
        row.shipment_id = ship.id
        row.received_on = today
        row.opened_on = None
        # Deliberately NOT last_seen_on: that field means "a back-room count saw
        # it on the shelf". A pack delivered this morning and already in a box by
        # tonight must still come up missing in tonight's count.
        _db().commit()

        held = len(_held_by_game().get(tc.game_number, []))
        name = (_catalog().games.get(tc.game_number).name
                if _catalog().games.get(tc.game_number) else "")
        return jsonify({"ok": True, "game": tc.game_number, "pack": tc.pack,
                        "name": name, "held": held,
                        "message": f"{tc.game_number} pack {tc.pack} received"
                                   f" — {held} now in back stock",
                        "count": _db().query(PackRow).filter(
                            PackRow.shipment_id == ship.id).count()})

    # --- the nightly backstock count --------------------------------------
    @app.get("/backstock/count")
    @perm_required("backstock")
    def backstock_count():
        """Scan every unopened pack in the back room, in any order.

        Unlike the box walk there's no prescribed sequence — the back room isn't
        ordered. Finishing is what matters: whatever wasn't scanned is no longer
        unopened, which is how a pack that went into a box between two counts
        gets noticed even if nobody scanned it on the way.
        """
        today = _today(_store_tz())
        seen = [r for r in _packs(state="backstock") if r.last_seen_on == today]
        expected = _packs(state="backstock")
        return render_template("backstock_count.html", today=today,
                               seen=seen, expected=expected)

    @app.post("/api/backstock/count")
    @perm_required("backstock")
    def api_backstock_count():
        """Mark one pack as still sitting unopened in the back room."""
        raw = (request.get_json(silent=True) or {}).get("raw", "")
        try:
            tc = parse_ticket(raw, _known_games())
        except BarcodeError as e:
            return jsonify({"ok": False, "message": f"not a ticket barcode ({e})"})

        today = _today(_store_tz())
        row = _pack_row(tc.game_number, tc.pack)
        if row is None:
            # A pack nobody recorded receiving. Believe the shelf, not the record.
            row = _pack_row(tc.game_number, tc.pack, create=True, source="inferred")
            row.received_on = today
        elif row.state == "settled":
            return jsonify({"ok": False, "message":
                            f"pack {tc.pack} is marked settled and returned — "
                            "clear that first if it's really still here"})
        was = row.state
        row.state = "backstock"
        row.opened_on = None
        row.last_seen_on = today
        _db().commit()
        note = " (was recorded as in a box)" if was == "active" else ""
        return jsonify({"ok": True, "game": tc.game_number, "pack": tc.pack,
                        "message": f"{tc.game_number} pack {tc.pack} counted{note}",
                        "count": len([r for r in _packs(state="backstock")
                                      if r.last_seen_on == today])})

    @app.post("/backstock/count/finish")
    @perm_required("backstock")
    def backstock_count_finish():
        """Close the count: anything not scanned tonight has left the back room.

        That is the inference the whole thing exists for — a pack that is no
        longer unopened went into a box, and the day's sales have to account for
        it even though no box scan ever saw it.
        """
        today = _today(_store_tz())
        opened = []
        for row in _packs(state="backstock"):
            if row.last_seen_on == today:
                continue
            row.state = "active"
            row.opened_on = today
            opened.append(row)
        _db().commit()
        if opened:
            audit("backstock.count",
                  f"{len(opened)} pack(s) no longer in back stock: "
                  + ", ".join(f"{r.game_number}/{r.pack}" for r in opened[:12])
                  + ("…" if len(opened) > 12 else ""))
        else:
            audit("backstock.count", "back stock counted; nothing had moved")
        return redirect(url_for("backstock"))

    # --- settle & return ---------------------------------------------------
    @app.post("/packs/settle")
    @perm_required("settle")
    def pack_settle():
        """Record a pack as settled and returned, from a box or straight out of
        back stock. This is what "we pulled it" means on paper, so it is stamped
        with who, when and why — and only a superadmin can undo it.
        """
        game = (request.form.get("game") or "").strip()
        pack = (request.form.get("pack") or "").strip()
        slot = (request.form.get("slot") or "").strip() or None
        reason = (request.form.get("reason") or "other").strip()
        note = (request.form.get("note") or "").strip()
        if not game:
            abort(400, "which game?")

        row = _pack_row(game, pack) if pack else None
        if row is None:
            # Settling a pack that was never in back stock (it predates this, or
            # came straight from a box) still has to be recorded.
            row = PackRow(store=_store(), game_number=game, pack=pack or "",
                          source="inferred")
            _db().add(row)

        row.state = "settled"
        row.slot = slot or row.slot
        row.settled_on = _today(_store_tz())
        row.settled_at = datetime.now(timezone.utc)
        row.settled_by = session.get("staff_name") or session.get("username") or ""
        row.settle_reason = reason
        row.settle_note = note

        # A settled pack is out of the box it was in.
        if slot:
            _set_box(slot, None, source="manual")
        _db().commit()
        audit("pack.settle",
              f"{game}" + (f" pack {pack}" if pack else "")
              + (f" from box {slot}" if slot else " from back stock")
              + f" — {reason}" + (f": {note}" if note else ""))
        return redirect(request.form.get("next") or url_for("backstock"))

    @app.post("/packs/unsettle")
    @superadmin_required
    def pack_unsettle():
        """Undo a settlement entered by mistake. Superadmin only, on purpose:
        this is the record of money going back to the state, and a store should
        not be able to quietly rewrite it."""
        row = _db().get(PackRow, int(request.form.get("pack_id") or 0))
        if row is None:
            abort(404)
        was = f"{row.game_number}/{row.pack} settled {row.settled_on} by {row.settled_by}"
        row.state = "backstock" if not row.opened_on else "active"
        row.settled_on = row.settled_at = row.settled_by = None
        row.settle_reason = row.settle_note = None
        _db().commit()
        audit("pack.unsettle", f"cleared: {was}")
        return redirect(request.form.get("next") or url_for("backstock"))

    # --- history: sales over time, game data over time, audit trail --------
    def _store_log(slug=None) -> ScanLog:
        rows = _db().scalars(select(ScanRow).where(
            ScanRow.store == (slug or _store()))).all()
        return ScanLog(scans=[r.to_scan() for r in rows])

    @app.get("/history")
    @perm_required("history")
    def history():
        """Three views of the past, which answer different questions:
        what did we sell, how is a game decaying, and who changed what."""
        tab = request.args.get("tab") or "sales"
        tz = _store_tz()
        ctx = {"tab": tab, "days": [], "games": [], "audit": [], "trend": []}

        if tab == "sales":
            log = _store_log()
            prices = _load_prices()
            dates = sorted({business_date(sc.scanned_at, tz) for sc in log.scans},
                           reverse=True)[:60]
            for d in dates:
                rep = daily_report(log, d, prices=prices, store=_store(), tz=tz)
                if rep.rows:
                    ctx["days"].append(rep)
            # Per-game totals across the whole window: the real sell-through.
            totals: dict = {}
            for rep in ctx["days"]:
                for game, g in rep.per_game.items():
                    t = totals.setdefault(game, {"game": game, "tickets": 0, "revenue": 0.0,
                                                 "days": 0})
                    t["tickets"] += g["tickets"]
                    t["revenue"] += (g["revenue"] or 0)
                    t["days"] += 1
            for t in totals.values():
                t["per_day"] = round(t["tickets"] / t["days"], 2) if t["days"] else 0
                t["revenue"] = round(t["revenue"], 2)
            ctx["trend"] = sorted(totals.values(), key=lambda t: -t["per_day"])

        elif tab == "games":
            ctx["games"] = pa_data.game_history(DATA_DIR / "snapshots",
                                                inventory=_inventory(), limit=30)

        elif tab == "audit":
            ctx["audit"] = _db().scalars(
                select(AuditRow).where(AuditRow.store == _store())
                .order_by(AuditRow.at.desc()).limit(200)).all()

        return render_template("history.html", **ctx)

    def _recent_dates(today: str, days: int) -> list:
        """The `days` calendar dates ending the day BEFORE `today`."""
        try:
            d0 = datetime.strptime(today, "%Y-%m-%d").date()
        except ValueError:
            return []
        return [(d0 - timedelta(days=i)).isoformat() for i in range(1, days + 1)]

    # --- superadmin: stores, managers, chain overview ----------------------
    @app.get("/overview")
    @superadmin_required
    def overview():
        """Every store side by side — where the chain is leaking money."""
        stores = _db().scalars(select(Store).order_by(Store.name)).all()
        prices = _load_prices()
        cfg_obj = _cfg()
        rows = []
        for st in stores:
            inv = {r.game_number for r in _db().scalars(
                select(InventoryRow).where(InventoryRow.store == st.slug)).all()}
            emph = _db().get(EmphasisRow, st.slug)
            weights = cfg_obj.rating_weights.scaled(emph.to_emphasis() if emph else {})
            srows = pa_data.store_rows(_catalog(), inv, cfg_obj.thresholds, weights)
            summary = pa_data.store_summary(srows)

            log = _store_log(st.slug)
            today = _today(st.timezone)
            rep = daily_report(log, today, prices=prices, store=st.slug, tz=st.timezone)
            dates = sorted({business_date(sc.scanned_at, st.timezone) for sc in log.scans},
                           reverse=True)[:7]
            week_tickets = week_rev = 0
            for d in dates:
                r = daily_report(log, d, prices=prices, store=st.slug, tz=st.timezone)
                week_tickets += r.total_tickets
                week_rev += (r.total_revenue or 0)

            # Night-count compliance. Today is excluded — a store that hasn't
            # closed yet hasn't missed anything — and so is anything before the
            # store's first scan, since a store can't miss a count it wasn't
            # open for yet. A day in between with no scans at all IS a miss.
            first_day = min((business_date(sc.scanned_at, st.timezone)
                             for sc in log.scans), default=None)
            judged = [d for d in _recent_dates(today, 6)
                      if first_day and d >= first_day]
            missed = [d for d in judged
                      if not count_status(log, d, store=st.slug,
                                          tz=st.timezone)["night_done"]]
            rows.append({
                "store": st, "summary": summary,
                "today_tickets": rep.total_tickets, "today_revenue": rep.total_revenue,
                "week_tickets": week_tickets, "week_revenue": round(week_rev, 2),
                "staff": _db().query(StaffRow).filter(StaffRow.store == st.slug).count(),
                "last_count": max((sc.scanned_at for sc in log.scans), default=None),
                "today_counts": count_status(log, today, store=st.slug, tz=st.timezone),
                "missed_nights": len(missed), "judged_days": len(judged),
            })
        return render_template("overview.html", rows=rows)


    def _slugify(name: str) -> str:
        out = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
        return (out or "store")[:64]

    @app.route("/admin/stores", methods=["GET", "POST"])
    @superadmin_required
    def admin_stores():
        error = None
        if request.method == "POST":
            act = request.form.get("action") or "add"
            if act == "add":
                name = (request.form.get("name") or "").strip()
                slug = _slugify(request.form.get("slug") or name)
                if not name:
                    error = "Store name is required."
                elif _db().get(Store, slug) is not None:
                    error = f"A store with the id '{slug}' already exists."
                else:
                    _db().add(Store(
                        slug=slug, name=name,
                        timezone=(request.form.get("timezone") or "America/New_York").strip(),
                        slots=(request.form.get("slots") or "48").strip(),
                        address=(request.form.get("address") or "").strip() or None,
                        phone=(request.form.get("phone") or "").strip() or None,
                        retailer_number=(request.form.get("retailer_number") or "").strip() or None))
                    _db().commit()
                    audit("store.create", f"{name} ({slug})")
            elif act == "update":
                st = _db().get(Store, request.form.get("slug") or "")
                if st:
                    st.name = (request.form.get("name") or st.name).strip()
                    st.timezone = (request.form.get("timezone") or st.timezone).strip()
                    st.slots = (request.form.get("slots") or st.slots).strip()
                    st.address = (request.form.get("address") or "").strip() or None
                    st.phone = (request.form.get("phone") or "").strip() or None
                    st.retailer_number = (request.form.get("retailer_number") or "").strip() or None
                    st.active = request.form.get("active") == "on"
                    _db().commit()
                    audit("store.update", st.slug)
            elif act == "delete":
                st = _db().get(Store, request.form.get("slug") or "")
                typed = (request.form.get("confirm_name") or "").strip()
                if st is None:
                    error = "That store no longer exists."
                elif typed.lower() != (st.name or "").strip().lower():
                    error = (f"Type the store's name exactly ({st.name}) to delete it. "
                             "Nothing was deleted.")
                else:
                    error = _delete_store(st)
            if not error:
                return redirect(url_for("admin_stores"))
        stores = _db().scalars(select(Store).order_by(Store.name)).all()
        counts = {st.slug: _db().query(User).filter(User.store == st.slug).count()
                  for st in stores}
        return render_template("admin_stores.html", stores=stores, error=error,
                               manager_counts=counts,
                               contents={st.slug: _store_contents(st.slug) for st in stores})

    def _store_contents(slug: str) -> dict:
        """What a store is actually holding — shown before anyone deletes it, so
        the decision is made with the numbers in view rather than blind."""
        def n(model, field="store"):
            return _db().query(model).filter(getattr(model, field) == slug).count()
        return {
            "scans": n(ScanRow), "boxes": n(BoxRow), "games": n(InventoryRow),
            "staff": n(StaffRow), "packs": n(PackRow), "shipments": n(ShipmentRow),
            "managers": n(User),
        }

    def _delete_store(st: Store) -> str | None:
        """Delete a store and everything operational belonging to it.

        The audit and access logs are deliberately kept: they record what people
        did, and deleting a store is not a reason to lose that. Everything else —
        scans, boxes, carried games, staff PINs, packs, shipments, and the store's
        own manager accounts — goes, because it is meaningless without the store.

        Returns an error string, or None on success.
        """
        slug = st.slug
        counts = _store_contents(slug)

        # A superadmin deleting the store they're currently viewing would be left
        # pointed at nothing; drop the selection first.
        if session.get("acting_store") == slug:
            session.pop("acting_store", None)

        for model in (ScanRow, BoxRow, InventoryRow, StaffRow, PackRow, ShipmentRow,
                      ActiveCount, User):
            _db().query(model).filter(model.store == slug).delete(synchronize_session=False)
        _db().query(EmphasisRow).filter(EmphasisRow.store == slug).delete(
            synchronize_session=False)
        _db().delete(st)
        _db().commit()

        audit("store.delete",
              f"{st.name} ({slug}) deleted with "
              + ", ".join(f"{v} {k}" for k, v in counts.items() if v)
              if any(counts.values()) else f"{st.name} ({slug}) deleted (it was empty)")
        return None

    @app.route("/admin/users", methods=["GET", "POST"])
    @superadmin_required
    def admin_users():
        error = None
        if request.method == "POST":
            act = request.form.get("action") or "add"
            if act == "add":
                uname = (request.form.get("username") or "").strip().lower()
                pw = request.form.get("password") or ""
                store_slug = request.form.get("store") or None
                role = request.form.get("role") or "manager"
                if not re.fullmatch(r"[a-z0-9._-]{3,64}", uname):
                    error = "Username: 3-64 chars, letters/numbers/._- only."
                elif len(pw) < 6:
                    error = "Password must be at least 6 characters."
                elif _db().scalar(select(User).where(User.username == uname)):
                    error = f"'{uname}' is taken."
                elif role != "superadmin" and not store_slug:
                    error = "Pick a store for this manager."
                else:
                    _db().add(User(username=uname, password_hash=generate_password_hash(pw),
                                   role=role, store=None if role == "superadmin" else store_slug,
                                   display_name=(request.form.get("display_name") or "").strip() or None))
                    _db().commit()
                    audit("user.create", f"{uname} ({role}) -> {store_slug or 'all stores'}")
            elif act == "reset_pw":
                u = _db().get(User, int(request.form.get("user_id") or 0))
                pw = request.form.get("password") or ""
                if u and len(pw) >= 6:
                    u.password_hash = generate_password_hash(pw)
                    _db().commit()
                    audit("user.reset_password", u.username or "")
                else:
                    error = "Password must be at least 6 characters."
            elif act == "rename":
                u = _db().get(User, int(request.form.get("user_id") or 0))
                new = (request.form.get("username") or "").strip()
                if not u:
                    error = "No such account."
                elif not re.fullmatch(r"[A-Za-z0-9._-]{3,64}", new):
                    error = "Username: 3-64 chars, letters/numbers/._- only."
                elif _db().scalar(select(User).where(func.lower(User.username) == new.lower(),
                                                     User.id != u.id)):
                    error = f"'{new}' is taken."
                else:
                    old = u.username or u.email
                    u.username = new
                    _db().commit()
                    if u.id == session.get("user_id"):
                        session["username"] = new       # keep the header honest
                    audit("user.rename", f"{old} -> {new}")

            elif act == "delete":
                u = _db().get(User, int(request.form.get("user_id") or 0))
                if not u:
                    error = "No such account."
                elif u.id == session.get("user_id"):
                    error = "You can't delete the account you're signed in with."
                elif u.role == "superadmin" and _db().query(User).filter(
                        User.role == "superadmin").count() <= 1:
                    error = "That's the only superadmin — the chain would have no owner."
                else:
                    # A store's data is keyed by store slug, not by user, so
                    # removing a manager never takes inventory, staff or sales
                    # with it. The store simply has no manager until you add one.
                    name = u.username or u.email
                    _db().delete(u)
                    _db().commit()
                    audit("user.delete", name or "")

            elif act == "toggle":
                u = _db().get(User, int(request.form.get("user_id") or 0))
                if not u or u.id == session.get("user_id"):
                    error = "You can't disable the account you're signed in with."
                elif (u.active and u.role == "superadmin"
                      and _db().query(User).filter(User.role == "superadmin",
                                                   User.active.is_(True)).count() <= 1):
                    error = "That's the only active superadmin."
                else:
                    u.active = not bool(u.active)
                    _db().commit()
                    audit("user.toggle", f"{u.username} active={u.active}")
            if not error:
                return redirect(url_for("admin_users"))
        return render_template("admin_users.html",
                               users=_db().scalars(select(User).order_by(User.username)).all(),
                               stores=_db().scalars(select(Store).order_by(Store.name)).all(),
                               error=error, me=session.get("user_id"))

    @app.post("/admin/act-as")
    @superadmin_required
    def admin_act_as():
        """Switch which store the superadmin is operating on."""
        slug = request.form.get("store") or ""
        if _db().get(Store, slug):
            session["acting_store"] = slug
            session.pop("staff_id", None)      # a new store means a new PIN context
            session.pop("staff_name", None)
            session.pop("staff_role", None)
        return redirect(request.form.get("next") or url_for("dashboard"))

    @app.get("/access")
    @perm_required("access")
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

    def _box_map() -> dict:
        """slot -> BoxRow for this store."""
        rows = _db().scalars(select(BoxRow).where(BoxRow.store == _store())).all()
        return {r.slot: r for r in rows}

    def _inventory() -> set:
        """The games this store carries.

        Boxes are the source of truth for placement, but a game can also be
        tracked before it has a home (just added, not yet loaded), so the carried
        set is the union of both.
        """
        inv = {r.game_number for r in _db().scalars(
            select(InventoryRow).where(InventoryRow.store == _store())).all()}
        inv |= {r.game_number for r in _db().scalars(
            select(BoxRow).where(BoxRow.store == _store())).all() if r.game_number}
        return inv

    def _set_box(slot: str, game: str | None, source: str = "manual") -> None:
        """Put a game in a box (or empty it), keeping the carried set in step."""
        row = _db().scalar(select(BoxRow).where(BoxRow.store == _store(),
                                                BoxRow.slot == slot))
        if row is None:
            row = BoxRow(store=_store(), slot=slot)
            _db().add(row)
        previous = row.game_number
        row.game_number = game or None
        row.source = source
        row.updated_at = datetime.now(timezone.utc)

        if game:   # a game in a box is by definition carried
            exists = _db().scalar(select(InventoryRow).where(
                InventoryRow.store == _store(), InventoryRow.game_number == game))
            if not exists:
                _db().add(InventoryRow(store=_store(), game_number=game))
        _db().commit()

        # If nothing holds the old game any more, it is no longer carried.
        if previous and previous != game:
            still = _db().scalar(select(BoxRow).where(
                BoxRow.store == _store(), BoxRow.game_number == previous))
            if still is None:
                stale = _db().scalar(select(InventoryRow).where(
                    InventoryRow.store == _store(), InventoryRow.game_number == previous))
                if stale is not None:
                    _db().delete(stale)
                    _db().commit()

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

    def _night_reminder() -> dict | None:
        """A nudge for the one count that can't be skipped — but only once the
        day is far enough along that it's actually late. Nobody needs to be told
        at 9am that tonight's count hasn't happened."""
        tz = _store_tz()
        now = datetime.now(as_zone(tz) or timezone.utc)
        if now.hour < 17:
            return None
        st = count_status(_store_log(), _today(tz), store=_store(), tz=tz)
        return None if st["night_done"] else st

    @app.get("/dashboard")
    @login_required
    def dashboard():
        cat = _catalog()
        inv = _inventory()
        weights, cfg = _effective_weights()
        rows = pa_data.store_rows(cat, inv, cfg.thresholds, weights)
        return render_template(
            "dashboard.html", email=session.get("email"),
            night_due=_night_reminder(),
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
        """What is in each box — the store as it physically stands."""
        cat = _catalog()
        games = cat.games
        boxes = _box_map()
        weights, cfg = _effective_weights()
        rated = {r["game_number"]: r for r in
                 pa_data.store_rows(cat, _inventory(), cfg.thresholds, weights)}

        rows = []
        for slot in _store_slots():
            b = boxes.get(slot)
            num = b.game_number if b else None
            rows.append({
                "slot": slot, "game_number": num,
                "game": games.get(num) if num else None,
                "rated": rated.get(num),
                "source": b.source if b else None,
                "updated_at": b.updated_at if b else None,
            })

        # Games we carry that aren't in a box yet — just added, or pulled out.
        placed = {r["game_number"] for r in rows if r["game_number"]}
        unplaced = []
        for num in sorted(_inventory() - placed):
            g = games.get(num)
            unplaced.append({"game_number": num,
                             "name": g.name if g else "(not on PA active list)",
                             "price": g.price if g else None})
        filled = sum(1 for r in rows if r["game_number"])
        return render_template("inventory.html", rows=rows, unplaced=unplaced,
                               filled=filled, total=len(rows))

    @app.route("/inventory/box/<slot>", methods=["GET", "POST"])
    @perm_required("boxes")
    def inventory_box(slot):
        """Set or clear one box. Kept as its own small page so a phone shows a
        readable game list instead of 48 dropdowns on one screen."""
        if slot not in _store_slots():
            abort(404)
        if request.method == "POST":
            raw = (request.form.get("game_number") or "").strip()
            if raw in ("", "__clear__"):
                _set_box(slot, None)
                audit("box.clear", slot)
            else:
                tc = try_parse_ticket(raw, _known_games())   # a scan works here too
                num = tc.game_number if tc else raw
                _set_box(slot, num)
                audit("box.set", f"{slot} -> {num}")
            return redirect(url_for("inventory"))

        cat = _catalog()
        boxes = _box_map()
        current = boxes.get(slot)
        active = sorted((g for g in cat.games.values() if g.status == "active"),
                        key=lambda g: (-(g.price or 0), g.name or ""))
        # Which pack is in this box, so pulling it can be settled properly rather
        # than just blanked out of the map.
        in_box = None
        if current and current.game_number:
            in_box = _db().scalar(select(PackRow).where(
                PackRow.store == _store(), PackRow.slot == slot,
                PackRow.game_number == current.game_number,
                PackRow.state == "active").order_by(PackRow.id.desc()))

        # Why this box is green or red, right here. Chasing the same game through
        # the full catalog to find that out was the wrong shape for a phone.
        detail = _game_detail(current.game_number if current else None)

        return render_template("inventory_box.html", slot=slot, active=active,
                               current=current.game_number if current else None,
                               in_box=in_box, reasons=SETTLE_REASONS,
                               detail=detail, do=request.args.get("do") or "",
                               games=cat.games)

    def _price_ladder() -> list:
        """The price points PA actually sells at right now, cheapest first."""
        return sorted({g.price for g in _catalog().games.values()
                       if g.status == "active" and g.price})

    def _row_for(game_number: str) -> dict | None:
        """One game flattened the same way the dashboard flattens it."""
        weights, cfg = _effective_weights()
        rows = pa_data.store_rows(_catalog(), {game_number}, cfg.thresholds, weights)
        return rows[0] if rows else None

    def _swap_options(price, n: int = 2) -> dict:
        """The best games to put in this box instead.

        Same price first, because a $10 slot earns $10 a ticket and swapping it
        for a $2 game quietly cuts what that box takes. When nothing at that price
        is worth carrying — which is itself worth knowing, it means the whole
        price point is picked over — step one rung down or up the ladder and take
        the best from there.
        """
        weights, cfg = _effective_weights()
        cat, inv = _catalog(), _inventory()
        if not price:
            return {"options": [], "price_note": ""}

        same = pa_data.swap_targets(cat, inv, price, cfg.thresholds, weights, n=n)
        if same:
            return {"options": same[:n], "price_note": ""}

        ladder = _price_ladder()
        near = []
        if price in ladder:
            i = ladder.index(price)
            near = [p for p in (ladder[i - 1] if i > 0 else None,
                                ladder[i + 1] if i + 1 < len(ladder) else None) if p]
        else:   # an unusual price: fall back to the closest points either side
            near = sorted(ladder, key=lambda p: abs(p - price))[:2]

        pooled = []
        for p2 in near:
            for row in pa_data.swap_targets(cat, inv, p2, cfg.thresholds, weights, n=n):
                pooled.append(row)
        pooled.sort(key=lambda r: r["rating"], reverse=True)
        note = ""
        if pooled:
            shown = sorted({r["price"] for r in pooled[:n]})
            note = ("nothing worth carrying at ${:g} — these are ${}"
                    .format(price, " and $".join(f"{p:g}" for p in shown)))
        return {"options": pooled[:n], "price_note": note}

    def _game_detail(game_number: str | None) -> dict | None:
        """Everything behind one game's colour: the call, the rating, and each
        factor that voted — with the ones carrying no weight said so plainly."""
        if not game_number:
            return None
        cat = _catalog()
        g = cat.games.get(game_number)
        weights, cfg = _effective_weights()
        if g is None:
            # Same shape as a rated game: a missing catalog entry is a verdict
            # ("pull it"), not a reason for the page to fall over.
            return {"game_number": game_number, "name": "(not on PA's active list)",
                    "action": "send_back", "rating": None, "factors": [],
                    "reason": "not in the PA catalog any more — most likely ended; "
                              "check it and pull it",
                    "cutoff": weights.cutoff, "tiers": [], "price": None, "odds": None,
                    "mine": {}, "swaps": {"options": [], "price_note": ""},
                    "status": "unknown", "sell_through": None,
                    "top_prize_value": None, "top_prizes_remaining": None,
                    "jackpot_density": None, "jackpot_significant": False,
                    "sales_end_date": None}

        action, reason = recommendation(g, cfg.thresholds, weights)
        rating, factors = rate(g, weights)
        total = sum(f.weight for f in factors if f.score is not None and f.weight > 0)
        rows = []
        for f in factors:
            rows.append({
                "key": f.key, "label": f.label, "score": f.score, "detail": f.detail,
                "note": f.note,
                # Share of the decision this factor actually carried. A factor with
                # no data doesn't drag the game down — it simply doesn't vote.
                "share": (round(100 * f.weight / total) if f.score is not None
                          and f.weight > 0 and total else 0),
                "voted": f.score is not None and f.weight > 0,
            })
        # Only for a game on its way out: "what should go in here instead" is the
        # question a red box actually raises.
        swaps = _swap_options(g.price) if action == "send_back" else {"options": [],
                                                                      "price_note": ""}
        mine = _row_for(game_number) or {}

        return {
            "game_number": game_number, "name": g.name, "price": g.price,
            "odds": g.odds, "status": g.status, "action": action, "reason": reason,
            "rating": rating, "cutoff": weights.cutoff, "factors": rows,
            "mine": mine, "swaps": swaps,
            "tiers": g.tier_health(), "sell_through": g.sell_through_pct,
            "top_prize_value": g.top_prize_value,
            "top_prizes_remaining": g.top_prizes_remaining,
            "jackpot_density": g.jackpot_density,
            "jackpot_significant": g.jackpot_density_significant,
            "sales_end_date": g.sales_end_date,
        }

    @app.post("/inventory/add")
    @perm_required("boxes")
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
            audit("inventory.add", raw[:200])
        return redirect(request.form.get("next") or url_for("inventory"))

    @app.post("/inventory/remove")
    @perm_required("boxes")
    def inventory_remove():
        num = (request.form.get("game_number") or "").strip()
        row = _db().scalar(select(InventoryRow).where(
            InventoryRow.store == _store(), InventoryRow.game_number == num))
        if row:
            _db().delete(row)
            _db().commit()
            audit("inventory.remove", num)
        return redirect(request.form.get("next") or url_for("inventory"))

    @app.route("/weights", methods=["GET", "POST"])
    @perm_required("pricing")
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
    @perm_required("reports")
    def report():
        date = request.args.get("date") or _today(_store_tz())
        rows = _db().scalars(select(ScanRow).where(ScanRow.store == _store())).all()
        log = ScanLog(scans=[r.to_scan() for r in rows])
        try:
            resolver = Config.load(str(ROOT / "config.yaml")).pack_resolver()
        except Exception:  # noqa: BLE001 — report must render even without config
            resolver = None
        rep = daily_report(log, date, prices=_load_prices(), resolver=resolver,
                           store=_store(), tz=_store_tz(),
                           # Packs that came out of back stock but never showed
                           # up in a count still sold; this is what counts them.
                           packs_opened=_packs_opened_on(date))
        return render_template("report.html", date=date, report=rep,
                               counts=count_status(log, date, store=_store(),
                                                   tz=_store_tz()),
                               today=_today(_store_tz()),
                               md=render_daily_report_md(rep), email=session.get("email"))


def main():
    app = create_app()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=bool(os.environ.get("FLASK_DEBUG")))


if __name__ == "__main__":
    main()
