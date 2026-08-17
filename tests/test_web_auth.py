"""Two-layer auth: the store stays signed in; people unlock a shift with a PIN.

The counter device should never need the store password again once set up, and
every count should still say who did it.
"""

import pytest

from lottery_tracker.web.app import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app({
        "DATABASE_URL": f"sqlite:///{tmp_path/'a.db'}",
        "SECRET_KEY": "k", "DEFAULT_STORE": "t", "SLOTS": "3", "REGISTER_CODE": None,
    })
    app.config.update(TESTING=True)
    c = app.test_client()
    c.post("/register", data={"email": "store@x.com", "password": "pw"})
    return c


def _add_staff(client, name, pin):
    return client.post("/staff", data={"action": "add", "name": name, "pin": pin})


# --- logout confirmation ----------------------------------------------------

def test_logout_shows_a_confirmation_first(client):
    page = client.get("/logout")
    assert page.status_code == 200
    assert b"Sign the store out?" in page.data
    # ...and merely visiting it must NOT sign you out
    assert client.get("/dashboard").status_code == 200


def test_logout_only_happens_on_post(client):
    client.post("/logout")
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["Location"]


# --- PIN layer --------------------------------------------------------------

def test_scanning_works_with_no_staff_configured(client):
    """A fresh install shouldn't be locked out of its own scanner."""
    assert client.get("/count").status_code == 200


def test_once_staff_exist_scanning_requires_a_pin(client):
    _add_staff(client, "Priya", "1234")
    r = client.get("/count", follow_redirects=False)
    assert r.status_code == 302 and "/pin" in r.headers["Location"]
    # read-only pages stay open — no PIN needed to look at the dashboard
    assert client.get("/dashboard").status_code == 200


def test_correct_pin_unlocks_and_wrong_pin_does_not(client):
    _add_staff(client, "Priya", "1234")
    bad = client.post("/pin", data={"pin": "9999"})
    assert b"Wrong PIN" in bad.data
    assert client.get("/count", follow_redirects=False).status_code == 302

    ok = client.post("/pin", data={"pin": "1234"}, follow_redirects=False)
    assert ok.status_code == 302
    assert client.get("/count").status_code == 200


def test_pin_is_scoped_to_the_named_person(client):
    _add_staff(client, "Priya", "1234")
    _add_staff(client, "Sam", "5678")
    page = client.get("/pin").data.decode()
    sam_id = page.split('data-id="')[2].split('"')[0]   # second person listed
    # Sam's id with Priya's PIN must fail
    assert b"Wrong PIN" in client.post("/pin", data={"staff_id": sam_id, "pin": "1234"}).data
    assert client.post("/pin", data={"staff_id": sam_id, "pin": "5678"},
                       follow_redirects=False).status_code == 302


def test_scans_are_attributed_to_the_person(client):
    _add_staff(client, "Priya", "1234")
    client.post("/pin", data={"pin": "1234"})
    client.post("/count/start", json={"session": "morning"})
    client.post("/api/scan", json={"raw": "1742011331200893"})
    client.post("/api/commit")

    from lottery_tracker.web.models import ScanRow
    from sqlalchemy import select
    with client.application.config["SESSION_FACTORY"]() as db:
        rows = db.scalars(select(ScanRow)).all()
    assert rows and rows[0].user_email == "Priya"


def test_switch_user_keeps_the_store_signed_in(client):
    _add_staff(client, "Priya", "1234")
    client.post("/pin", data={"pin": "1234"})
    client.post("/pin/clear")
    # store session survives: dashboard still open, scanning asks for a PIN again
    assert client.get("/dashboard").status_code == 200
    assert client.get("/count", follow_redirects=False).status_code == 302


def test_pin_must_be_digits_and_reasonable_length(client):
    for bad in ["12", "abcd", "123456789"]:
        r = _add_staff(client, f"P{bad}", bad)
        assert b"PIN must be 4-8 digits" in r.data


def test_pins_are_hashed_not_stored_in_the_clear(client):
    _add_staff(client, "Priya", "1234")
    from lottery_tracker.web.models import StaffRow
    from sqlalchemy import select
    with client.application.config["SESSION_FACTORY"]() as db:
        row = db.scalars(select(StaffRow)).first()
    assert "1234" not in row.pin_hash


def test_remove_staff(client):
    _add_staff(client, "Priya", "1234")
    page = client.get("/staff").data.decode()
    sid = page.split('name="staff_id" value="')[1].split('"')[0]
    client.post("/staff", data={"action": "remove", "staff_id": sid})
    # check the people list, not the "Priya" placeholder in the add-person form
    people = client.get("/staff").data.decode().split("People (")[1]
    assert "Priya" not in people
    assert "No one yet" in people
