"""PIN-only front door + IP access logging.

PIN_ONLY removes the store email/password from the front end. That's a real
security reduction, so the access log is the compensating control: every request
and every PIN attempt is recorded with the client IP.
"""

import pytest
from sqlalchemy import select

from lottery_tracker.web.app import create_app
from lottery_tracker.web.models import AccessRow


def _app(tmp_path, **over):
    cfg = {"DATABASE_URL": f"sqlite:///{tmp_path/'p.db'}", "SECRET_KEY": "k",
           "DEFAULT_STORE": "t", "SLOTS": "3", "REGISTER_CODE": None}
    cfg.update(over)
    app = create_app(cfg)
    app.config.update(TESTING=True)
    return app


@pytest.fixture()
def pin_app(tmp_path):
    return _app(tmp_path, PIN_ONLY=True)


@pytest.fixture()
def pin_client(pin_app):
    return pin_app.test_client()


def _rows(app, **where):
    with app.config["SESSION_FACTORY"]() as db:
        q = select(AccessRow)
        for k, v in where.items():
            q = q.where(getattr(AccessRow, k) == v)
        return db.scalars(q).all()


# --- PIN-only front door ----------------------------------------------------

def test_no_staff_yet_site_is_reachable_to_bootstrap(pin_client):
    """With no PINs created there'd be no way in at all — so allow it, which is
    exactly why the access log matters."""
    assert pin_client.get("/staff").status_code == 200


def test_front_door_is_the_pin_pad_not_a_password(pin_client):
    pin_client.post("/staff", data={"action": "add", "name": "Priya", "pin": "1234"})
    r = pin_client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302 and "/pin" in r.headers["Location"]
    # No store credentials are asked for — no email field, no login link.
    page = pin_client.get("/pin").data.decode()
    assert 'name="email"' not in page
    assert "/login" not in page


def test_pin_signs_you_all_the_way_in(pin_client):
    pin_client.post("/staff", data={"action": "add", "name": "Priya", "pin": "1234"})
    assert pin_client.post("/pin", data={"pin": "1234"},
                           follow_redirects=False).status_code == 302
    assert pin_client.get("/dashboard").status_code == 200
    assert pin_client.get("/count").status_code == 200


def test_logout_only_ends_the_shift_in_pin_mode(pin_client):
    pin_client.post("/staff", data={"action": "add", "name": "Priya", "pin": "1234"})
    pin_client.post("/pin", data={"pin": "1234"})
    page = pin_client.get("/logout").data.decode()
    assert "End" in page and "store email and password" not in page
    pin_client.post("/logout")
    assert pin_client.get("/count", follow_redirects=False).status_code == 302


def test_password_mode_is_unaffected(tmp_path):
    """The default must still be the password gate."""
    c = _app(tmp_path, PIN_ONLY=False).test_client()
    r = c.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["Location"]


@pytest.mark.parametrize("val,expected_login_path", [
    ("true", "/pin"), ("1", "/pin"), ("yes", "/pin"), ('"true"', "/pin"),
    ("false", "/login"), ("", "/login"), (None, "/login"),
])
def test_pin_only_env_parsing(tmp_path, monkeypatch, val, expected_login_path):
    if val is None:
        monkeypatch.delenv("PIN_ONLY", raising=False)
    else:
        monkeypatch.setenv("PIN_ONLY", val)
    app = create_app({"DATABASE_URL": f"sqlite:///{tmp_path/'e.db'}",
                      "SECRET_KEY": "k", "DEFAULT_STORE": "t", "SLOTS": "3",
                      "REGISTER_CODE": None})
    app.config.update(TESTING=True)
    c = app.test_client()
    # A gate only applies once someone can actually sign in, so create a PIN
    # first (a store with no staff is deliberately open, to bootstrap).
    c.post("/staff", data={"action": "add", "name": "P", "pin": "1234"})
    r = c.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302
    assert expected_login_path in r.headers["Location"]


# --- access logging ---------------------------------------------------------

def test_requests_are_logged_with_the_client_ip(pin_app):
    c = pin_app.test_client()
    c.get("/staff", headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
    rows = _rows(pin_app, path="/staff")
    assert rows and rows[0].ip == "203.0.113.9"   # left-most = real client, not the proxy


def test_health_checks_are_not_logged(pin_app):
    c = pin_app.test_client()
    c.get("/healthz")
    assert _rows(pin_app, path="/healthz") == []


def test_failed_pin_attempts_are_recorded(pin_app):
    c = pin_app.test_client()
    c.post("/staff", data={"action": "add", "name": "Priya", "pin": "1234"})
    c.post("/pin", data={"pin": "0000"}, headers={"X-Forwarded-For": "198.51.100.7"})
    fails = _rows(pin_app, event="pin_fail")
    assert len(fails) == 1
    assert fails[0].ip == "198.51.100.7"


def test_successful_pin_is_recorded_with_the_person(pin_app):
    c = pin_app.test_client()
    c.post("/staff", data={"action": "add", "name": "Priya", "pin": "1234"})
    c.post("/pin", data={"pin": "1234"})
    ok = _rows(pin_app, event="pin_ok")
    assert len(ok) == 1 and ok[0].staff_name == "Priya"


def test_access_page_surfaces_failures_and_devices(pin_app):
    c = pin_app.test_client()
    c.post("/staff", data={"action": "add", "name": "Priya", "pin": "1234"})
    c.post("/pin", data={"pin": "9999"}, headers={"X-Forwarded-For": "198.51.100.7"})
    c.post("/pin", data={"pin": "1234"})
    page = c.get("/access").data.decode()
    assert "198.51.100.7" in page       # the unfamiliar device is visible
    assert "FAILED PIN" in page
    assert "failed PIN attempt" in page
