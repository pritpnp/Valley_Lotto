"""Box-level inventory: which game is in which dispenser box.

The point of modelling boxes rather than a flat list of games is that a count
already knows the answer — every scan carries its slot and its game — so the map
maintains itself instead of drifting out of date.
"""

import pytest
from sqlalchemy import select

from lottery_tracker.web.app import create_app
from lottery_tracker.web.models import BoxRow, InventoryRow


@pytest.fixture()
def app(tmp_path):
    a = create_app({"DATABASE_URL": f"sqlite:///{tmp_path/'b.db'}", "SECRET_KEY": "k",
                    "DEFAULT_STORE": "hq", "SLOTS": "4", "REGISTER_CODE": None})
    a.config.update(TESTING=True)
    return a


@pytest.fixture()
def client(app):
    c = app.test_client()
    c.post("/register", data={"username": "boss", "password": "secret1"})
    return c


def _boxes(app):
    with app.config["SESSION_FACTORY"]() as db:
        return {r.slot: (r.game_number, r.source) for r in db.scalars(select(BoxRow)).all()}


def _carried(app):
    with app.config["SESSION_FACTORY"]() as db:
        return {r.game_number for r in db.scalars(select(InventoryRow)).all()}


# --- the page ---------------------------------------------------------------

def test_boxes_page_lists_every_box(client):
    html = client.get("/inventory").data.decode()
    for n in ("1", "2", "3", "4"):
        assert f"BOX {n}" in html
    assert "0 of 4 filled" in html


def test_an_unknown_box_is_404(client):
    assert client.get("/inventory/box/99").status_code == 404


# --- setting a box ----------------------------------------------------------

def test_setting_a_box_also_marks_the_game_carried(app, client):
    client.post("/inventory/box/2", data={"game_number": "1750"})
    assert _boxes(app)["2"] == ("1750", "manual")
    assert "1750" in _carried(app)
    assert "1 of 4 filled" in client.get("/inventory").data.decode()


def test_a_box_can_be_set_by_scanning_a_ticket(app, client):
    """The clerk has a gun in hand — scanning the box's own ticket is the
    fastest way to say what's in it."""
    client.post("/inventory/box/1", data={"game_number": "1742011331200893"})
    assert _boxes(app)["1"][0] == "1742"     # the game, not the raw barcode


def test_emptying_a_box_drops_the_game_when_nothing_else_holds_it(app, client):
    client.post("/inventory/box/1", data={"game_number": "1750"})
    client.post("/inventory/box/1", data={"game_number": "__clear__"})
    assert _boxes(app)["1"][0] is None
    assert "1750" not in _carried(app)


def test_a_game_in_two_boxes_survives_emptying_one(app, client):
    """Popular games occupy several boxes; clearing one must not untrack it."""
    client.post("/inventory/box/1", data={"game_number": "1750"})
    client.post("/inventory/box/3", data={"game_number": "1750"})
    client.post("/inventory/box/1", data={"game_number": "__clear__"})
    assert _boxes(app)["3"][0] == "1750"
    assert "1750" in _carried(app)           # still in box 3


def test_replacing_a_game_untracks_the_old_one(app, client):
    client.post("/inventory/box/1", data={"game_number": "1750"})
    client.post("/inventory/box/1", data={"game_number": "1744"})
    carried = _carried(app)
    assert "1744" in carried and "1750" not in carried


# --- the self-maintaining part ----------------------------------------------

def test_committing_a_count_fills_in_the_box_map(app, client):
    """This is the whole point: you never have to tell it what's in a box,
    because counting already told it."""
    client.post("/count/start", json={"session": "morning"})
    client.post("/api/scan", json={"raw": "1742011331200893"})   # box 1
    client.post("/api/scan", json={"raw": "1750-0091798-010"})   # box 2
    client.post("/api/commit")

    boxes = _boxes(app)
    assert boxes["1"] == ("1742", "scan")
    assert boxes["2"] == ("1750", "scan")
    assert {"1742", "1750"} <= _carried(app)


def test_a_count_corrects_a_box_that_was_swapped(app, client):
    """A game was pulled and replaced without anyone updating the app — the next
    count fixes the record."""
    client.post("/inventory/box/1", data={"game_number": "1750"})
    client.post("/count/start", json={"session": "morning"})
    client.post("/api/scan", json={"raw": "1742011331200893"})   # box 1 now holds 1742
    client.post("/api/commit")

    assert _boxes(app)["1"] == ("1742", "scan")
    assert "1750" not in _carried(app)       # nothing holds it any more


def test_the_page_marks_which_boxes_came_from_a_scan(client):
    client.post("/count/start", json={"session": "morning"})
    client.post("/api/scan", json={"raw": "1742011331200893"})
    client.post("/api/commit")
    assert "from a scan" in client.get("/inventory").data.decode()


def test_boxes_are_isolated_between_stores(app, client):
    client.post("/admin/stores", data={"action": "add", "name": "Second Store", "slots": "4"})
    client.post("/inventory/box/1", data={"game_number": "1750"})   # at the default store
    client.post("/admin/act-as", data={"store": "second-store"})
    html = client.get("/inventory").data.decode()
    assert "1750" not in html
    assert "0 of 4 filled" in html
