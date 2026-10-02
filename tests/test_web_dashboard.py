"""Tests for the unified dashboard pages (KEEP/SEND-BACK, catalog, inventory, emphasis).

These assert the Flask port *uses* the shared rating engine rather than
re-deriving it: a rating rendered here must equal ``lottery_tracker.rules.rate``.
"""

import json

import pytest

from lottery_tracker.web.app import create_app
from lottery_tracker.rules import RatingWeights, rate, recommendation, Thresholds
from lottery_app import pa_data


@pytest.fixture()
def app(tmp_path, monkeypatch):
    application = create_app({
        "DATABASE_URL": f"sqlite:///{tmp_path/'t.db'}",
        "SECRET_KEY": "test-key", "DEFAULT_STORE": "t", "SLOTS": "A:2",
        "REGISTER_CODE": None,
    })
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    c = app.test_client()
    c.post("/register", data={"email": "o@x.com", "password": "pw"})
    return c


def test_dashboard_requires_login(app):
    r = app.test_client().get("/dashboard", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_dashboard_empty_inventory_renders(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert b"No games yet" in r.data


def _carried_numbers(client):
    """The games this store carries, read from the database.

    Deliberately not scraped from the page: the inventory UI is box-based now,
    and a test of *what is carried* shouldn't break when the layout changes.
    """
    from lottery_tracker.web.models import InventoryRow
    from sqlalchemy import select
    with client.application.config["SESSION_FACTORY"]() as db:
        return {r.game_number for r in db.scalars(select(InventoryRow)).all()}


def test_inventory_add_multiple_and_remove(client):
    client.post("/inventory/add", data={"game_number": "1750 1744, 1780"})
    assert {"1750", "1744", "1780"} <= _carried_numbers(client)
    client.post("/inventory/remove", data={"game_number": "1744"})
    carried = _carried_numbers(client)
    assert "1744" not in carried
    assert {"1750", "1780"} <= carried        # the others survive


def test_inventory_add_is_idempotent(client):
    from lottery_tracker.web.models import InventoryRow
    from sqlalchemy import select
    client.post("/inventory/add", data={"game_number": "1750"})
    client.post("/inventory/add", data={"game_number": "1750"})
    with client.application.config["SESSION_FACTORY"]() as db:
        rows = [r for r in db.scalars(select(InventoryRow)).all() if r.game_number == "1750"]
    assert len(rows) == 1                        # no duplicate row


def test_dashboard_rating_matches_the_shared_engine(client):
    """The page must show exactly what lottery_tracker.rules computes."""
    cat = pa_data.load_catalog("data/state.json")
    active = [g for g in cat.games.values() if g.status == "active"][:3]
    if not active:
        pytest.skip("no active games in state.json")
    for g in active:
        client.post("/inventory/add", data={"game_number": g.game_number})

    html = client.get("/dashboard").data.decode()
    w = RatingWeights()  # neutral sliders => base config weights
    for g in active:
        expected, _ = rate(g, w)
        if expected is not None:
            assert f"{expected:.0f}" in html


def test_the_rating_page_explains_and_has_nothing_to_tune(client):
    page = client.get("/weights").data.decode()
    assert "How games are rated" in page
    assert "5% of every ticket" in page
    assert 'type="range"' not in page                 # no sliders any more
    # Posting old slider values changes nothing and isn't accepted.
    assert client.post("/weights", data={"odds": "3"}).status_code == 405


def test_the_rating_page_lists_the_typical_sales_at_each_price(client, tmp_path, monkeypatch):
    from _games import selling_dict
    state = {"captured_at": "2026-10-01T00:00:00Z", "games": {
        n: selling_dict({"game_number": n, "name": f"G{n}", "price": 5, "status": "active"}, d)
        for n, d in (("1", 10_000), ("2", 30_000), ("3", 50_000))}}
    import json
    p = tmp_path / "state.json"
    p.write_text(json.dumps(state))
    from lottery_app import pa_data as pd
    real = pd.load_catalog
    monkeypatch.setattr(pd, "load_catalog", lambda _path: real(p))
    page = client.get("/weights").data.decode()
    assert "$5 games (3)" in page
    assert "typical $30K/day · send back under $6,000/day" in page


def test_catalog_renders_and_marks_carried(client):
    cat = pa_data.load_catalog("data/state.json")
    active = [g for g in cat.games.values() if g.status == "active"]
    if not active:
        pytest.skip("no active games in state.json")
    client.post("/inventory/add", data={"game_number": active[0].game_number})
    r = client.get("/catalog")
    assert r.status_code == 200
    # the catalog marks what you already carry (a pill now, not a star)
    assert b"you carry it" in r.data


def test_unknown_game_shows_as_pull_it(client):
    client.post("/inventory/add", data={"game_number": "9999"})   # valid shape, not a real game
    html = client.get("/dashboard").data.decode()
    assert "SEND BACK" in html
    assert "not found in PA catalog" in html


# --- scanning a ticket into the inventory box -------------------------------
REAL_GUN = "1742011331200893"   # real gun output: game 1742, pack 0113312, tkt 008


def test_scanning_a_ticket_into_inventory_adds_the_game(client):
    """A clerk with the gun will scan a ticket here — store the GAME, not the
    16-digit barcode (which previously landed as a bogus 'not on PA list' row)."""
    client.post("/inventory/add", data={"game_number": REAL_GUN})
    carried = _carried_numbers(client)
    assert "1742" in carried
    assert REAL_GUN not in carried


def test_inventory_ignores_junk_tokens(client):
    client.post("/inventory/add", data={"game_number": "hello ?? 12"})
    assert _carried_numbers(client) == set()


def test_inventory_mixed_typed_and_scanned(client):
    client.post("/inventory/add", data={"game_number": f"1750 {REAL_GUN}, 1744"})
    assert {"1750", "1742", "1744"} <= _carried_numbers(client)
