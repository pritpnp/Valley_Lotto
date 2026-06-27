"""FastAPI app: multi-store scratch-off dashboard.

Run it (from the repo root):

    PYTHONPATH=src uvicorn lottery_app.main:app --host 0.0.0.0 --port 8000

Environment:
    LOTTO_DB      path to the SQLite app database   (default data/app.db)
    LOTTO_STATE   path to the scraper's state.json   (default data/state.json)
    LOTTO_SECRET  secret key for signing session cookies (REQUIRED in production)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from lottery_tracker.config import Config
from lottery_tracker.rules import Thresholds

from . import db
from .auth import hash_password, verify_password
from .pa_data import (
    bring_in_candidates,
    load_catalog,
    new_games,
    store_rows,
    store_summary,
)

# --- paths / config ---------------------------------------------------------
# Resolved at call time (not import time) so the running process — and tests —
# always see the current environment.
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]  # src/lottery_app -> repo root
SECRET = os.environ.get("LOTTO_SECRET", "dev-insecure-change-me")


def _db_path() -> str:
    return os.environ.get("LOTTO_DB", str(REPO / "data" / "app.db"))


def _state_path() -> str:
    return os.environ.get("LOTTO_STATE", str(REPO / "data" / "state.json"))


def _config_path() -> str:
    return os.environ.get("LOTTO_CONFIG", str(REPO / "config.yaml"))


templates = Jinja2Templates(directory=str(HERE / "templates"))
app = FastAPI(title="Valley Lotto")
app.add_middleware(SessionMiddleware, secret_key=SECRET, max_age=60 * 60 * 12)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def _config() -> Config:
    try:
        return Config.load(_config_path())
    except Exception:  # noqa: BLE001 — config is optional for the app
        return Config({})


def _thresholds() -> Thresholds:
    return _config().thresholds


# --- request-scoped DB connection ------------------------------------------
def get_db():
    conn = db.connect(_db_path())
    try:
        db.init_db(conn)
        yield conn
    finally:
        conn.close()


# --- auth helpers -----------------------------------------------------------
def current_user(request: Request, conn) -> dict | None:
    uid = request.session.get("user_id")
    if not uid:
        return None
    row = db.get_user(conn, uid)
    return dict(row) if row else None


def require_user(request: Request, conn=Depends(get_db)) -> dict:
    user = current_user(request, conn)
    if not user:
        raise HTTPException(status_code=303, detail="login", headers={"Location": "/login"})
    return user


def active_store_id(request: Request, user: dict) -> int:
    """Which store the user is currently looking at.

    Non-admins are pinned to their own store. Admins may switch and the choice is
    remembered in the session.
    """
    if user["role"] != "admin":
        return user["store_id"]
    return int(request.session.get("active_store_id") or user["store_id"])


# --- exception handler so require_user can redirect to /login ---------------
@app.exception_handler(HTTPException)
async def _auth_redirect(request: Request, exc: HTTPException):
    if exc.status_code == 303 and exc.headers and exc.headers.get("Location"):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>", status_code=exc.status_code)


# --- routes -----------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request, conn=Depends(get_db)):
    return RedirectResponse("/dashboard" if current_user(request, conn) else "/login", 303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, conn=Depends(get_db), error: str = ""):
    stores = [dict(s) for s in db.list_stores(conn)]
    return templates.TemplateResponse(
        request, "login.html", {"stores": stores, "error": error}
    )


@app.post("/login")
def login_submit(
    request: Request,
    store_id: int = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    conn=Depends(get_db),
):
    user = db.find_user(conn, store_id, username.strip())
    if not user or not verify_password(password, user["password_hash"]):
        return RedirectResponse("/login?error=Invalid+login", 303)
    request.session["user_id"] = user["id"]
    request.session["active_store_id"] = user["store_id"]
    return RedirectResponse("/dashboard", 303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, conn=Depends(get_db), user: dict = Depends(require_user)):
    sid = active_store_id(request, user)
    store = db.get_store(conn, sid)
    inv = db.get_inventory(conn, sid)
    catalog = load_catalog(_state_path())
    cfg = _config()
    th = cfg.thresholds
    rows = store_rows(catalog, inv, th)
    summary = store_summary(rows)
    fresh = new_games(catalog, within_days=14)
    bring_in = bring_in_candidates(
        catalog, inv, th, min_left=cfg.bring_in_min_left, per_price=cfg.bring_in_per_price
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user, "store": dict(store) if store else None,
            "stores": [dict(s) for s in db.list_stores(conn)],
            "rows": rows, "summary": summary, "fresh": fresh, "bring_in": bring_in,
            "captured_at": catalog.captured_at, "active_count": catalog.active_count,
        },
    )


@app.post("/switch-store")
def switch_store(request: Request, store_id: int = Form(...),
                 conn=Depends(get_db), user: dict = Depends(require_user)):
    if user["role"] == "admin":
        request.session["active_store_id"] = store_id
    return RedirectResponse("/dashboard", 303)


@app.get("/inventory", response_class=HTMLResponse)
def inventory_page(request: Request, conn=Depends(get_db), user: dict = Depends(require_user)):
    sid = active_store_id(request, user)
    store = db.get_store(conn, sid)
    inv = db.get_inventory(conn, sid)
    catalog = load_catalog(_state_path())
    # Show carried games (named) + the rest of the active catalog to add.
    carried = []
    for num in sorted(inv):
        g = catalog.games.get(num)
        carried.append({"game_number": num, "name": g.name if g else "(unknown)",
                        "price": g.price if g else None})
    carried.sort(key=lambda r: (-(r["price"] or 0), r["game_number"]))
    available = [
        {"game_number": g.game_number, "name": g.name, "price": g.price}
        for g in catalog.games.values()
        if g.status == "active" and g.game_number not in inv
    ]
    available.sort(key=lambda r: (-(r["price"] or 0), r["game_number"]))
    return templates.TemplateResponse(
        request,
        "inventory.html",
        {"user": user, "store": dict(store) if store else None,
         "carried": carried, "available": available},
    )


@app.post("/inventory/add")
def inventory_add(request: Request, game_number: str = Form(...),
                  conn=Depends(get_db), user: dict = Depends(require_user)):
    sid = active_store_id(request, user)
    for num in re.split(r"[\s,]+", game_number.strip()):
        if num:
            db.add_inventory(conn, sid, num)
    return RedirectResponse("/inventory", 303)


@app.post("/inventory/remove")
def inventory_remove(request: Request, game_number: str = Form(...),
                     conn=Depends(get_db), user: dict = Depends(require_user)):
    sid = active_store_id(request, user)
    db.remove_inventory(conn, sid, game_number)
    return RedirectResponse("/inventory", 303)


# --- admin: stores & users --------------------------------------------------
def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "store"


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, conn=Depends(get_db), user: dict = Depends(require_user)):
    if user["role"] not in ("admin", "owner"):
        raise HTTPException(403, "You don't have access to store administration.")
    if user["role"] == "admin":
        stores = [dict(s) for s in db.list_stores(conn)]
        users_by_store = {s["id"]: [dict(u) for u in db.list_users(conn, s["id"])] for s in stores}
    else:  # owner: only their own store
        store = db.get_store(conn, user["store_id"])
        stores = [dict(store)] if store else []
        users_by_store = {user["store_id"]: [dict(u) for u in db.list_users(conn, user["store_id"])]}
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"user": user, "stores": stores, "users_by_store": users_by_store},
    )


@app.post("/admin/store")
def admin_create_store(request: Request, name: str = Form(...),
                       conn=Depends(get_db), user: dict = Depends(require_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Only an administrator can create stores.")
    slug = _slug(name)
    if not db.get_store_by_slug(conn, slug):
        db.create_store(conn, slug, name.strip())
    return RedirectResponse("/admin", 303)


@app.post("/admin/user")
def admin_create_user(
    request: Request, store_id: int = Form(...), username: str = Form(...),
    password: str = Form(...), role: str = Form("staff"),
    conn=Depends(get_db), user: dict = Depends(require_user),
):
    # owners may only add users to their own store and may not mint admins.
    if user["role"] == "owner" and (store_id != user["store_id"] or role == "admin"):
        raise HTTPException(403, "Owners can only manage staff in their own store.")
    if user["role"] not in ("admin", "owner"):
        raise HTTPException(403, "No access.")
    if role not in ("admin", "owner", "staff"):
        role = "staff"
    if not db.find_user(conn, store_id, username.strip()):
        db.create_user(conn, store_id, username.strip(), hash_password(password), role)
    return RedirectResponse("/admin", 303)


@app.post("/admin/user/delete")
def admin_delete_user(request: Request, user_id: int = Form(...),
                      conn=Depends(get_db), user: dict = Depends(require_user)):
    target = db.get_user(conn, user_id)
    if not target:
        return RedirectResponse("/admin", 303)
    if user["role"] == "owner" and target["store_id"] != user["store_id"]:
        raise HTTPException(403, "No access.")
    if user["role"] not in ("admin", "owner"):
        raise HTTPException(403, "No access.")
    if target["id"] != user["id"]:  # never delete yourself
        db.delete_user(conn, user_id)
    return RedirectResponse("/admin", 303)


@app.post("/admin/user/password")
def admin_reset_password(request: Request, user_id: int = Form(...), password: str = Form(...),
                         conn=Depends(get_db), user: dict = Depends(require_user)):
    target = db.get_user(conn, user_id)
    if not target:
        return RedirectResponse("/admin", 303)
    if user["role"] == "owner" and target["store_id"] != user["store_id"]:
        raise HTTPException(403, "No access.")
    if user["role"] not in ("admin", "owner"):
        raise HTTPException(403, "No access.")
    db.set_password(conn, user_id, hash_password(password))
    return RedirectResponse("/admin", 303)


@app.get("/healthz")
def healthz():
    return {"ok": True}
