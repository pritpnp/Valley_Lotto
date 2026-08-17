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
    assert b"No games in your inventory" in r.data


def _carried_numbers(client):
    """Game numbers listed in the 'Carried' card (not the placeholder or the
    'Available to add' list)."""
    import re
    html = client.get("/inventory").data.decode()
    carried = html.split("Carried (")[1].split("Available to add")[0]
    return set(re.findall(r'name="game_number" value="(\d+)"', carried))


def test_inventory_add_multiple_and_remove(client):
    client.post("/inventory/add", data={"game_number": "1750 1744, 1780"})
    assert {"1750", "1744", "1780"} <= _carried_numbers(client)
    client.post("/inventory/remove", data={"game_number": "1744"})
    carried = _carried_numbers(client)
    assert "1744" not in carried
    assert {"1750", "1780"} <= carried        # the others survive


def test_inventory_add_is_idempotent(client):
    client.post("/inventory/add", data={"game_number": "1750"})
    client.post("/inventory/add", data={"game_number": "1750"})
    html = client.get("/inventory").data.decode()
    carried = html.split("Carried (")[1].split("Available to add")[0]
    assert carried.count('value="1750"') == 1   # no duplicate row


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


def test_emphasis_slider_changes_the_rating(client):
    """Moving a slider must actually re-weight the decision."""
    cat = pa_data.load_catalog("data/state.json")
    active = [g for g in cat.games.values() if g.status == "active"][:8]
    if not active:
        pytest.skip("no active games in state.json")
    for g in active:
        client.post("/inventory/add", data={"game_number": g.game_number})

    before = client.get("/dashboard").data
    client.post("/weights", data={"odds": "3", "prizes_left": "-3", "low_prize": "0",
                                  "low_prize_skew": "0", "jackpot_density": "0"})
    after = client.get("/dashboard").data
    assert before != after, "slider change had no effect on the dashboard"

    # ...and the saved notches come back on the form.
    page = client.get("/weights").data.decode()
    assert 'name="odds" min="-3" max="3" step="1" value="3"' in page


def test_emphasis_clamped_to_range(client):
    client.post("/weights", data={"odds": "99", "prizes_left": "-99", "low_prize": "0",
                                  "low_prize_skew": "0", "jackpot_density": "0"})
    page = client.get("/weights").data.decode()
    assert 'name="odds" min="-3" max="3" step="1" value="3"' in page
    assert 'name="prizes_left" min="-3" max="3" step="1" value="-3"' in page


def test_catalog_renders_and_marks_carried(client):
    cat = pa_data.load_catalog("data/state.json")
    active = [g for g in cat.games.values() if g.status == "active"]
    if not active:
        pytest.skip("no active games in state.json")
    client.post("/inventory/add", data={"game_number": active[0].game_number})
    r = client.get("/catalog")
    assert r.status_code == 200
    assert "★".encode() in r.data


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
