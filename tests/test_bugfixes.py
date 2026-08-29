"""Regression tests for bugs found in the deployed app.

Each of these silently produced WRONG NUMBERS rather than an error, which is the
dangerous kind — a store would have trusted the report.
"""

import pytest
from sqlalchemy import select

from lottery_tracker.scans import Scan, ScanLog, learn_pack_size
from lottery_tracker.reporting import daily_report, business_date, local_time
from lottery_tracker.web.app import create_app
from lottery_tracker.web.models import ActiveCount


# --- Bug A: a night count landed on the next day (UTC vs the store's day) ----

def _evening_log():
    """A PA store: 8am and 9pm local on Aug 17 — which is 12:00Z and 01:00Z(+1)."""
    return ScanLog(scans=[
        Scan.from_raw("1750-0091798-010", scanned_at="2026-08-17T12:00:00Z",
                      slot="1", session="morning"),
        Scan.from_raw("1750-0091798-025", scanned_at="2026-08-18T01:00:00Z",
                      slot="1", session="night"),
    ])


def test_evening_count_belongs_to_the_local_day():
    r = daily_report(_evening_log(), "2026-08-17", prices={"1750": 30.0},
                     tz="America/New_York")
    assert r.total_tickets == 15          # was 0: the day's sales vanished
    assert r.total_revenue == 450.0
    # ...and nothing leaks into the next day
    assert daily_report(_evening_log(), "2026-08-18", prices={"1750": 30.0},
                        tz="America/New_York").total_tickets == 0


def test_counts_display_in_store_local_time():
    r = daily_report(_evening_log(), "2026-08-17", tz="America/New_York")
    assert [c["at_local"] for c in r.rows[0].counts] == ["08:00", "21:00"]


@pytest.mark.parametrize("iso,tz,expected", [
    ("2026-08-18T01:00:00Z", "America/New_York", "2026-08-17"),   # 9pm previous day
    ("2026-08-17T12:00:00Z", "America/New_York", "2026-08-17"),
    ("2026-01-15T02:00:00Z", "America/New_York", "2026-01-14"),   # EST, not EDT
    ("2026-08-17T12:00:00Z", None, "2026-08-17"),                 # no tz => UTC prefix
    ("garbage", "America/New_York", "garbage"[:10]),              # never raises
])
def test_business_date(iso, tz, expected):
    assert business_date(iso, tz) == expected


def test_bad_timezone_name_does_not_break_reporting():
    r = daily_report(_evening_log(), "2026-08-17", tz="Not/AZone")
    assert r is not None      # falls back to UTC rather than raising


# --- Bug B: one misread scan poisoned a game's learned pack size ------------

def test_a_single_misread_does_not_define_the_pack_size():
    good = [Scan.from_raw(f"1750-0091798-{t:03d}", scanned_at=f"2026-08-1{i}T12:00:00Z")
            for i, t in enumerate((5, 12, 19))]
    assert learn_pack_size(good) == 20
    poisoned = good + [Scan.from_raw("1750-0091798-399", scanned_at="2026-08-14T12:00:00Z")]
    assert learn_pack_size(poisoned) == 20     # was 400


def test_a_pack_seen_rolling_over_is_the_strongest_signal():
    """Pack A ran to ticket 059 then the slot moved to pack B -> A held 60."""
    scans = [
        Scan.from_raw("1744-0100200-050", scanned_at="2026-08-10T12:00:00Z"),
        Scan.from_raw("1744-0100200-059", scanned_at="2026-08-11T12:00:00Z"),
        Scan.from_raw("1744-0100201-004", scanned_at="2026-08-12T12:00:00Z"),
    ]
    assert learn_pack_size(scans) == 60


def test_learned_size_never_exceeds_a_real_pack():
    absurd = [Scan.from_raw("1750-0091798-350", scanned_at="2026-08-10T12:00:00Z")]
    assert learn_pack_size(absurd) <= 300


# --- Bug C: everyone shared one in-progress count in PIN-only mode ----------

@pytest.fixture()
def pin_app(tmp_path):
    app = create_app({"DATABASE_URL": f"sqlite:///{tmp_path/'c.db'}", "SECRET_KEY": "k",
                      "DEFAULT_STORE": "t", "SLOTS": "4", "REGISTER_CODE": None,
                      "PIN_ONLY": True})
    app.config.update(TESTING=True)
    return app


def test_two_people_can_count_at_once_without_clobbering(pin_app):
    setup = pin_app.test_client()
    # The first person must be a manager — once an employee PINs in they can no
    # longer add staff, which is the point of the role split.
    setup.post("/staff", data={"action": "add", "name": "Priya", "pin": "1111",
                               "role": "manager"})
    setup.post("/pin", data={"pin": "1111"})
    setup.post("/staff", data={"action": "add", "name": "Sam", "pin": "2222"})

    priya, sam = pin_app.test_client(), pin_app.test_client()
    priya.post("/pin", data={"pin": "1111"})
    sam.post("/pin", data={"pin": "2222"})

    priya.post("/count/start", json={"session": "morning"})
    sam.post("/count/start", json={"session": "morning"})
    priya.post("/api/scan", json={"raw": "1742011331200893"})   # Priya does box 1

    # Sam must still be on box 1, untouched by Priya's scan.
    assert sam.get("/api/state").get_json()["current_slot"] == "1"
    assert priya.get("/api/state").get_json()["current_slot"] == "2"

    with pin_app.config["SESSION_FACTORY"]() as db:
        owners = {r.user_email for r in db.scalars(select(ActiveCount)).all()}
    assert owners == {"Priya", "Sam"}      # was a single shared row keyed None
