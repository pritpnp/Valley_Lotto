"""The day's three counts: night is required, morning recommended, midday optional.

Also covers the rename of the old "evening" label onto "night".
"""

import pytest

from lottery_tracker.reporting import (SESSIONS, count_status, normalize_session,
                                       session_meta, daily_report)
from lottery_tracker.scans import Scan, ScanLog
from lottery_tracker.web.app import create_app


def _scan(session, slot, ticket, at, store="t"):
    return Scan(game_number="1750", pack="0091798", ticket=ticket, scanned_at=at,
                store=store, slot=slot, session=session)


# --- policy ------------------------------------------------------------------

def test_the_three_counts_carry_their_requirement():
    by_key = {s["key"]: s for s in SESSIONS}
    assert by_key["night"]["requirement"] == "required"
    assert by_key["morning"]["requirement"] == "recommended"
    assert by_key["midday"]["requirement"] == "optional"


def test_evening_is_just_the_old_name_for_night():
    assert normalize_session("evening") == "night"
    assert normalize_session("Evening ") == "night"
    assert session_meta("evening")["requirement"] == "required"
    # afternoon likewise folds onto midday
    assert normalize_session("afternoon") == "midday"


def test_an_unknown_label_survives_as_an_optional_extra():
    assert normalize_session("shift-change") == "shift-change"
    assert session_meta("shift-change")["requirement"] == "optional"


# --- count_status ------------------------------------------------------------

def test_a_day_with_only_a_morning_count_is_short_a_night_count():
    log = ScanLog(scans=[_scan("morning", "1", 10, "2026-08-19T12:00:00Z")])
    st = count_status(log, "2026-08-19", store="t")
    assert st["night_done"] is False
    assert [s["key"] for s in st["overdue"]] == ["night"]
    assert {s["key"] for s in st["missing"]} == {"night"}   # midday never owed


def test_a_night_count_clears_the_requirement_but_morning_is_still_nudged():
    log = ScanLog(scans=[_scan("night", "1", 40, "2026-08-19T23:00:00Z")])
    st = count_status(log, "2026-08-19", store="t")
    assert st["night_done"] is True
    assert st["overdue"] == []
    assert [s["key"] for s in st["missing"]] == ["morning"]


def test_an_evening_labelled_count_satisfies_the_night_requirement():
    """Counts taken before the rename must not read as a missed night."""
    log = ScanLog(scans=[_scan("evening", "1", 40, "2026-08-19T23:00:00Z")])
    st = count_status(log, "2026-08-19", store="t")
    assert st["night_done"] is True
    assert st["overdue"] == []


def test_status_reports_when_who_and_how_many_boxes():
    log = ScanLog(scans=[
        _scan("night", "1", 40, "2026-08-19T23:00:00Z"),
        _scan("night", "2", 12, "2026-08-19T23:01:00Z"),
    ])
    log.scans[0].user = log.scans[1].user = "Ravi"
    st = count_status(log, "2026-08-19", store="t", tz="America/New_York")
    night = next(s for s in st["sessions"] if s["key"] == "night")
    assert night["boxes"] == 2 and night["by"] == "Ravi"
    assert night["at_local"] == "19:00"          # 23:00Z is 7pm ET


def test_status_is_per_store_and_per_day():
    log = ScanLog(scans=[
        _scan("night", "1", 40, "2026-08-19T23:00:00Z", store="a"),
        _scan("night", "1", 40, "2026-08-19T23:00:00Z", store="b"),
    ])
    assert count_status(log, "2026-08-19", store="a")["night_done"] is True
    assert count_status(log, "2026-08-18", store="a")["night_done"] is False


def test_night_is_offered_first_and_midday_last():
    st = count_status(ScanLog(scans=[]), "2026-08-19", store="t")
    ranked = sorted(st["sessions"], key=lambda s: s["rank"])
    assert [s["key"] for s in ranked] == ["night", "morning", "midday"]


# --- the report still orders morning -> midday -> night ----------------------

def test_an_evening_count_still_sorts_after_morning_in_the_report():
    log = ScanLog(scans=[
        _scan("morning", "1", 10, "2026-08-19T12:00:00Z"),
        _scan("evening", "1", 40, "2026-08-19T23:00:00Z"),
    ])
    rep = daily_report(log, "2026-08-19", store="t")
    assert rep.total_tickets == 30
    assert [c["session"] for c in rep.rows[0].counts] == ["morning", "evening"]


# --- the web app -------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    app = create_app({
        "DATABASE_URL": f"sqlite:///{tmp_path/'t.db'}",
        "SECRET_KEY": "k", "DEFAULT_STORE": "t", "SLOTS": "A:2",
        "REGISTER_CODE": None,
    })
    app.config.update(TESTING=True)
    c = app.test_client()
    c.post("/register", data={"username": "owner", "password": "pw"})
    return c


def test_starting_an_evening_count_records_it_as_night(client):
    s = client.post("/count/start", json={"session": "evening"}).get_json()
    assert s["session"] == "night"


def test_a_count_with_no_session_defaults_to_the_required_one(client):
    s = client.post("/count/start", json={}).get_json()
    assert s["session"] == "night"


def test_the_count_page_flags_the_missing_night_count(client):
    html = client.get("/count").data.decode()
    assert "Required" in html and "night count" in html


def test_the_count_page_shows_a_night_count_once_it_is_taken(client):
    client.post("/count/start", json={"session": "night"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})
    client.post("/api/scan", json={"raw": "1744-0100200-005"})
    client.post("/api/commit")
    html = client.get("/count").data.decode()
    assert "done" in html
    assert "hasn't been done today" not in html


def test_the_report_warns_when_the_night_count_is_missing(client):
    client.post("/count/start", json={"session": "morning"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})
    client.post("/api/scan", json={"raw": "1744-0100200-005"})
    client.post("/api/commit")
    html = client.get("/report").data.decode()
    assert "night count" in html
