"""Env-var robustness: a hosting dashboard must not be able to break the app.

Railway's raw editor accepts `KEY="value"`. If the quotes survive into the
process environment, naive parsing would crash the app on boot (int('"48"')).
These tests pin the tolerant behavior.
"""

import pytest

from lottery_tracker.web.app import create_app, _parse_slots, _env


@pytest.mark.parametrize("spec,first,last,count", [
    ("48", "1", "48", 48),
    ('"48"', "1", "48", 48),          # quoted, as a dashboard may pass it
    ("'48'", "1", "48", 48),
    ("  48  ", "1", "48", 48),
    ("A:24,B:24", "A1", "B24", 48),
    ('"A:24,B:24"', "A1", "B24", 48),
    ("A:2, B:2", "A1", "B2", 4),
])
def test_parse_slots_variants(spec, first, last, count):
    slots = _parse_slots(spec)
    assert len(slots) == count
    assert slots[0] == first and slots[-1] == last


@pytest.mark.parametrize("bad", ["", None, "abc", "A:xx", "!!!", ":,:"])
def test_parse_slots_bad_input_falls_back_not_crashes(bad):
    slots = _parse_slots(bad)
    assert slots == [str(i) for i in range(1, 49)]   # default 1..48


def test_env_strips_quotes(monkeypatch):
    monkeypatch.setenv("VL_TEST", '"VALLEY-J0RTG8"')
    assert _env("VL_TEST") == "VALLEY-J0RTG8"
    monkeypatch.setenv("VL_TEST", "plain")
    assert _env("VL_TEST") == "plain"
    assert _env("VL_TEST_MISSING", "fallback") == "fallback"


def test_app_boots_with_quoted_env(tmp_path, monkeypatch):
    """The exact shape of the user's Railway variables, quotes included."""
    monkeypatch.setenv("DATABASE_URL", f'"sqlite:///{tmp_path}/q.db"')
    monkeypatch.setenv("SECRET_KEY", '"11c62a6b0184c46d40342d14e7122f24c8f6f1c53b8bf5f28679e21e4322bb84"')
    monkeypatch.setenv("REGISTER_CODE", '"VALLEY-J0RTG8"')
    monkeypatch.setenv("DEFAULT_STORE", '"valley"')
    monkeypatch.setenv("SLOTS", '"48"')

    app = create_app()          # must not raise
    app.config.update(TESTING=True)
    assert len(app.config["SLOTS"]) == 48
    assert app.config["REGISTER_CODE"] == "VALLEY-J0RTG8"   # no stray quotes
    assert app.config["DEFAULT_STORE"] == "valley"

    c = app.test_client()
    assert c.get("/healthz").get_json() == {"ok": True}
    # Registration works with the code as typed by a human (no quotes).
    r = c.post("/register", data={"email": "a@b.com", "password": "pw",
                                  "code": "VALLEY-J0RTG8"}, follow_redirects=False)
    assert r.status_code == 302, "quoted REGISTER_CODE must still accept the plain code"
