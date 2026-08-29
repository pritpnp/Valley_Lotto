"""End-to-end tests for the Flask scan app: register, login, guided count, commit, report."""

import pytest

from lottery_tracker.web.app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app({
        "DATABASE_URL": f"sqlite:///{tmp_path/'t.db'}",
        "SECRET_KEY": "test-key",
        "DEFAULT_STORE": "t",
        "SLOTS": "A:2",           # tiny 2-box layout for fast tests
        "REGISTER_CODE": None,
    })
    app.config.update(TESTING=True)
    return app.test_client()


def _register(client, username="owner", pw="pw"):
    """Bootstrap the first account, which becomes the superadmin."""
    return client.post("/register", data={"username": username, "password": pw},
                       follow_redirects=False)


def test_auth_required_and_register_login(client):
    # protected page bounces to login
    r = client.get("/count", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    # register logs you in
    assert _register(client).status_code == 302
    assert client.get("/count").status_code == 200
    # logout then bad login
    client.post("/logout")
    bad = client.post("/login", data={"username": "owner", "password": "nope"})
    assert b"Invalid username or password" in bad.data


def test_full_count_commit_and_report(client):
    _register(client)

    # MORNING count
    s = client.post("/count/start", json={"session": "morning"}).get_json()
    assert s["current_slot"] == "A1" and s["total"] == 2

    s = client.post("/api/scan", json={"raw": "1750-0091798-010"}).get_json()
    assert s["step"]["ok"] and s["current_slot"] == "A2"
    s = client.post("/api/scan", json={"raw": "1744-0100200-005"}).get_json()
    assert s["complete"] is True
    assert client.post("/api/commit").get_json()["committed"] == 2

    # NIGHT count (same day) -> now we have deltas
    client.post("/count/start", json={"session": "night"})
    client.post("/api/scan", json={"raw": "1750-0091798-025"})
    client.post("/api/scan", json={"raw": "1744-0100200-040"})
    client.post("/api/commit")

    # Report: A1 sold 15, A2 sold 35 -> 50 total
    html = client.get("/report").data
    assert b"50" in html
    assert b"A1" in html and b"A2" in html


def test_bad_barcode_rejected_via_api(client):
    _register(client)
    client.post("/count/start", json={"session": "morning"})
    s = client.post("/api/scan", json={"raw": "garbage"}).get_json()
    assert s["step"]["ok"] is False
    assert s["current_slot"] == "A1"          # did not advance


def test_rescan_and_back_via_api(client):
    _register(client)
    client.post("/count/start", json={"session": "morning"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})   # A1
    s = client.post("/api/scan", json={"raw": "1744-0100200-005"}).get_json()  # A2 -> complete
    assert s["complete"]
    # fix A1 without losing place
    s = client.post("/api/rescan", json={"slot": "A1", "raw": "1750-0091798-012"}).get_json()
    assert s["step"]["ok"]
    a1 = next(b for b in s["slots"] if b["slot"] == "A1")
    assert a1["ticket"] == 12


def test_active_session_survives_reload(client):
    _register(client)
    client.post("/count/start", json={"session": "morning"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})
    # A fresh state fetch (as a page reload would do) still knows we're on A2.
    s = client.get("/api/state").get_json()
    assert s["current_slot"] == "A2" and s["done"] == 1


# --- the count ends by itself; duplicates are confirmed by re-scanning -------

def test_the_walk_ending_is_what_saves_the_count(client):
    """There is no Finish button — going through every box saves it."""
    _register(client)
    client.post("/count/start", json={"session": "morning"})
    s = client.post("/api/scan", json={"raw": "1742011331200893"}).get_json()
    assert s["walk_done"] is False
    s = client.post("/api/scan", json={"raw": "1750-0091798-010"}).get_json()
    assert s["walk_done"] is True          # the client commits on seeing this
    assert client.post("/api/commit").get_json()["committed"] == 2


def test_an_empty_box_is_skipped_and_still_ends_the_walk(client):
    _register(client)
    client.post("/count/start", json={"session": "morning"})
    client.post("/api/scan", json={"raw": "1742011331200893"})
    s = client.post("/api/skip").get_json()      # this box is empty
    assert s["walk_done"] is True
    assert s["complete"] is False                # not every box has a ticket
    assert client.post("/api/commit").get_json()["committed"] == 1


def test_a_repeated_game_is_held_then_accepted_on_the_second_scan(client):
    _register(client)
    client.post("/count/start", json={"session": "morning"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})

    held = client.post("/api/scan", json={"raw": "1750-0091798-044"}).get_json()
    assert held["step"]["needs_confirm"] is True
    assert "scanned in box" in held["step"]["message"]
    assert held["current_slot"] == "A2"          # did not advance

    ok = client.post("/api/scan", json={"raw": "1750-0091798-044"}).get_json()
    assert ok["step"]["ok"] is True
    assert ok["walk_done"] is True               # that was the last box
