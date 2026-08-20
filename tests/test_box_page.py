"""The per-box screen: four actions, and the reason for the colour in place."""

import json
import pathlib

import pytest

from lottery_tracker.web.app import create_app


@pytest.fixture()
def app(tmp_path):
    a = create_app({"DATABASE_URL": f"sqlite:///{tmp_path/'x.db'}", "SECRET_KEY": "k",
                    "DEFAULT_STORE": "t", "SLOTS": "4", "REGISTER_CODE": None})
    a.config.update(TESTING=True)
    return a


@pytest.fixture()
def client(app):
    c = app.test_client()
    c.post("/register", data={"username": "prit", "password": "pw"})
    return c


@pytest.fixture()
def a_real_game():
    """A game number that exists in the bundled PA catalog."""
    raw = json.loads(pathlib.Path("data/state.json").read_text())
    games = raw.get("games") or raw
    for num, g in games.items():
        if g.get("status") == "active":
            return num
    pytest.skip("no active game in the bundled catalog")


def _fill(client, slot, game):
    return client.post(f"/inventory/box/{slot}", data={"game_number": game},
                       follow_redirects=True)


def test_the_box_page_offers_four_actions(client, a_real_game):
    _fill(client, "1", a_real_game)
    html = client.get("/inventory/box/1").data.decode()
    for label in ("Override", "Pick another game", "Empty", "Settle &amp; return"):
        assert label in html, label


def test_an_empty_box_offers_only_the_two_that_make_sense(client):
    html = client.get("/inventory/box/2").data.decode()
    assert "Override" in html and "Pick another game" in html
    assert "Settle &amp; return" not in html      # nothing in there to settle
    assert ">Empty" not in html


def test_each_action_opens_on_its_own(client, a_real_game):
    _fill(client, "1", a_real_game)
    over = client.get("/inventory/box/1?do=override").data.decode()
    assert 'name="game_number"' in over and "Pick an active game" not in over

    pick = client.get("/inventory/box/1?do=pick").data.decode()
    assert "Pick an active game" in pick

    settle = client.get("/inventory/box/1?do=settle").data.decode()
    assert 'name="reason"' in settle and "Why is it going back?" in settle


def test_the_rating_breakdown_is_on_the_box_page(client, a_real_game):
    _fill(client, "1", a_real_game)
    html = client.get("/inventory/box/1").data.decode()
    assert "Why this box is" in html
    assert "of the decision" in html               # each factor's share
    assert "Win odds" in html and "Low-prize stock" in html
    assert "Prizes left" in html


def test_the_breakdown_explains_density_rather_than_just_printing_it(client, a_real_game):
    _fill(client, "1", a_real_game)
    html = client.get("/inventory/box/1").data.decode()
    assert "Jackpot density" in html
    assert "ratio" in html and "not significant" in html


def test_a_factor_with_no_data_says_so_instead_of_scoring_zero(client, a_real_game):
    _fill(client, "1", a_real_game)
    html = client.get("/inventory/box/1").data.decode()
    assert "didn&#39;t vote" in html or "didn't vote" in html


def test_a_game_that_left_the_catalog_is_explained_not_crashed(client):
    _fill(client, "1", "9999")
    html = client.get("/inventory/box/1").data.decode()
    assert "not on PA" in html or "not in the PA catalog" in html


def test_the_breakdown_is_not_a_horizontally_scrolling_table(client, a_real_game):
    _fill(client, "1", a_real_game)
    html = client.get("/inventory/box/1").data.decode()
    where = html.index("Why this box is")
    assert "<table" not in html[where:]


def test_settle_is_hidden_from_someone_without_that_grant(client, a_real_game):
    _fill(client, "1", a_real_game)
    client.post("/staff", data={"action": "add", "name": "Sam", "pin": "1111",
                                "role": "employee", "perm": ["boxes"]},
                follow_redirects=True)
    client.post("/pin", data={"pin": "1111"})
    html = client.get("/inventory/box/1").data.decode()
    assert "Override" in html
    assert "Settle &amp; return" not in html


def test_the_count_page_carries_the_missing_box_guard(client):
    """The walk must not be able to end quietly short."""
    html = client.get("/count").data.decode()
    assert "boxes have nothing recorded" in html
    assert "Go scan them" in html


def test_the_server_reports_which_boxes_are_still_empty(client):
    client.post("/count/start", json={"session": "night"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})
    client.post("/api/skip")
    client.post("/api/skip")
    s = client.post("/api/scan", json={"raw": "1744-0100200-005"}).get_json()
    assert s["walk_done"] is True
    assert s["pending"] == ["2", "3"]        # what the guard lists
