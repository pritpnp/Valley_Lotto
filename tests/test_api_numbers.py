"""The store TVs' numbers feed: public, cache-safe, and quiet in the access log."""

import json
from datetime import datetime, timezone

import pytest

from lottery_tracker.web.app import create_app
from lottery_tracker.pa_feeds.store import FEED_ORDER, set_setting
from lottery_tracker.web.models import AccessRow, DrawResultRow


@pytest.fixture()
def app(tmp_path):
    a = create_app({"DATABASE_URL": f"sqlite:///{tmp_path/'n.db'}", "SECRET_KEY": "k",
                    "DEFAULT_STORE": "t", "SLOTS": "4", "REGISTER_CODE": None})
    a.config.update(TESTING=True)
    with a.config["SESSION_FACTORY"]() as db:
        db.add(DrawResultRow(
            source="pa_feed", game_id="8", title="Cash 5", draw_date="2026-10-28",
            numbers="05 11 15 18 35", extras="[]", jackpot_date="2026-10-29",
            jackpot_text="$1,700,000", jackpot_amount=1700000.0, raw="<game id='8'/>"))
        set_setting(db, FEED_ORDER, json.dumps(["8"]), datetime.now(timezone.utc))  # as each run does
        db.commit()
    return a


def test_anyone_can_read_it_without_signing_in(app):
    r = app.test_client().get("/api/numbers")
    assert r.status_code == 200
    body = r.get_json()
    cash5 = [g for g in body["games"] if g["id"] == "8"]
    assert cash5 and cash5[0]["numbers"] == ["05", "11", "15", "18", "35"]
    assert cash5[0]["jackpot"] == {"text": "$1,700,000", "amount": 1700000}


def test_it_is_safe_to_cache_and_to_read_from_another_site(app):
    r = app.test_client().get("/api/numbers")
    assert r.headers["Access-Control-Allow-Origin"] == "*"
    assert r.headers["Cache-Control"] == "public, max-age=60"
    # A shared cache must never hand one visitor's cookie to another.
    assert "Set-Cookie" not in r.headers


def test_it_never_sends_the_session_cookie_even_to_someone_signed_in(app):
    # A browser signed in to the app holds the 90-day login cookie, and Flask
    # sends a signed-in visitor's cookie back on every answer. On this one,
    # marked as fine for a shared cache to keep, it would be the store's login
    # handed to whoever the cache answers next.
    c = app.test_client()
    c.post("/register", data={"username": "prit", "password": "pw"})
    assert c.get_cookie("session") is not None                  # signed in
    assert "Set-Cookie" in c.get("/count").headers              # pages do refresh it
    r = c.get("/api/numbers")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "public, max-age=60"
    assert "Set-Cookie" not in r.headers
    assert "Cookie" not in r.headers.get("Vary", "")             # the same for everyone
    assert c.get("/count").status_code == 200                   # and still signed in


def test_a_failure_never_sends_the_session_cookie_either(app, monkeypatch):
    import lottery_tracker.pa_feeds.display as display
    monkeypatch.setattr(display, "build_payload", lambda session, now=None: 1 / 0)
    c = app.test_client()
    c.post("/register", data={"username": "prit", "password": "pw"})
    r = c.get("/api/numbers")
    assert r.status_code == 503 and "Set-Cookie" not in r.headers


def test_it_carries_no_store_data(app):
    # Sign in and set up a store, then make sure none of it leaks into the feed.
    c = app.test_client()
    c.post("/register", data={"username": "prit", "password": "pw"})
    text = json.dumps(app.test_client().get("/api/numbers").get_json())
    assert "prit" not in text and "store" not in text.lower().replace("pbmm_source", "")


def test_tv_checks_are_not_written_to_the_access_log(app):
    c = app.test_client()
    for _ in range(5):
        c.get("/api/numbers")
    with app.config["SESSION_FACTORY"]() as db:
        assert db.query(AccessRow).filter(AccessRow.path == "/api/numbers").count() == 0


def test_a_failure_says_so_and_is_never_cached(app, monkeypatch):
    import lottery_tracker.pa_feeds.display as display

    def broken(session, now=None):
        raise RuntimeError("database down")
    monkeypatch.setattr(display, "build_payload", broken)
    r = app.test_client().get("/api/numbers")
    assert r.status_code == 503
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["Access-Control-Allow-Origin"] == "*"
    assert "numbers" not in (r.get_json() or {})
