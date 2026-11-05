"""PA announces a game's end of sales weeks ahead: the game page says it in
the right tense ("stops selling ... in 15 days", then "stopped selling")."""

import json
import pathlib

import pytest

from lottery_tracker.web.app import _sales_end, create_app


def test_a_date_ahead_counts_down_and_a_date_passed_is_past():
    assert _sales_end("11/20/2026 12:00:00 AM", "2026-11-05") == {"date": "Nov 20, 2026", "days": 15}
    assert _sales_end("11/20/2026 12:00:00 AM", "2026-11-19")["days"] == 1
    assert _sales_end("11/20/2026 12:00:00 AM", "2026-11-20")["days"] == 0
    assert _sales_end("11/20/2026 12:00:00 AM", "2026-11-21")["days"] == -1
    assert _sales_end("2026-11-20", "2026-11-05") == {"date": "Nov 20, 2026", "days": 15}


def test_anything_that_is_not_a_date_is_none():
    for text in (None, "", "TBD", "13/45/2026", "Nov 20"):
        assert _sales_end(text, "2026-11-05") is None


def _page(tmp_path, monkeypatch, today):
    import lottery_tracker.web.app as web
    games = json.loads(pathlib.Path("data/state.json").read_text())["games"]
    num = next((n for n, g in games.items() if g.get("status") == "active" and g.get("sales_end_date")), None)
    if num is None:
        pytest.skip("no active game with an end-of-sales date in the bundled catalog")
    monkeypatch.setattr(web, "_today", lambda tz=None: today)
    app = create_app({"DATABASE_URL": f"sqlite:///{tmp_path/'g.db'}", "SECRET_KEY": "k",
                      "DEFAULT_STORE": "t", "SLOTS": "4", "REGISTER_CODE": None})
    app.config.update(TESTING=True)
    c = app.test_client()
    c.post("/register", data={"username": "prit", "password": "pw"})
    end = _sales_end(games[num]["sales_end_date"], "2000-01-01")["date"]
    return " ".join(c.get(f"/catalog/{num}").data.decode().split()), end


def test_the_game_page_says_it_will_stop_before_the_date(tmp_path, monkeypatch):
    html, end = _page(tmp_path, monkeypatch, "2000-01-01")
    assert f"PA stops selling this on {end}</b> (in " in html
    assert "stopped selling" not in html and "12:00:00 AM" not in html


def test_the_game_page_says_it_stopped_after_the_date(tmp_path, monkeypatch):
    html, end = _page(tmp_path, monkeypatch, "2099-01-01")
    assert f"PA stopped selling this on {end}" in html
