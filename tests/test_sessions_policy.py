"""The day's three counts: night is required, morning recommended, afternoon optional.

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
    assert by_key["afternoon"]["requirement"] == "optional"


def test_evening_is_just_the_old_name_for_night():
    assert normalize_session("evening") == "night"
    assert normalize_session("Evening ") == "night"
    assert session_meta("evening")["requirement"] == "required"
    # midday was the old name for the afternoon count
    assert normalize_session("midday") == "afternoon"


def test_an_unknown_label_survives_as_an_optional_extra():
    assert normalize_session("shift-change") == "shift-change"
    assert session_meta("shift-change")["requirement"] == "optional"


# --- count_status ------------------------------------------------------------

def test_a_day_with_only_a_morning_count_is_short_a_night_count():
    log = ScanLog(scans=[_scan("morning", "1", 10, "2026-08-19T12:00:00Z")])
    st = count_status(log, "2026-08-19", store="t")
    assert st["night_done"] is False
    assert [s["key"] for s in st["overdue"]] == ["night"]
    assert {s["key"] for s in st["missing"]} == {"night"}   # afternoon never owed


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


def test_night_is_offered_first_and_afternoon_last():
    st = count_status(ScanLog(scans=[]), "2026-08-19", store="t")
    ranked = sorted(st["sessions"], key=lambda s: s["rank"])
    assert [s["key"] for s in ranked] == ["night", "morning", "afternoon"]


# --- the report still orders morning -> afternoon -> night -------------------

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


def test_the_chain_overview_shows_todays_counts_per_store(client):
    client.post("/count/start", json={"session": "night"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})
    client.post("/api/scan", json={"raw": "1744-0100200-005"})
    client.post("/api/commit")
    html = client.get("/overview").data.decode()
    assert "Counts today" in html and "Missed nights" in html
    # a brand-new store has no earlier days to have missed
    assert "of 6" not in html


# --- an unfinished count is offered, never resumed behind your back ----------

def test_an_unfinished_count_is_offered_not_resumed(client):
    client.post("/count/start", json={"session": "afternoon"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})
    html = client.get("/count").data.decode()
    assert "count in progress" in html
    assert "Resume it" in html and "Start a different count" in html


def test_discarding_lets_a_different_count_be_started(client):
    client.post("/count/start", json={"session": "afternoon"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})
    assert client.post("/count/discard").get_json()["discarded"] is True
    s = client.post("/count/start", json={"session": "night"}).get_json()
    assert s["session"] == "night" and s["done"] == 0


def test_a_midday_labelled_count_reads_as_the_afternoon_one(client):
    s = client.post("/count/start", json={"session": "midday"}).get_json()
    assert s["session"] == "afternoon"


# --- the double-scan crash ---------------------------------------------------

def test_saving_a_count_twice_is_not_an_error(client):
    """A double-tapped save used to raise, which reached the phone as a 500 and
    a white screen."""
    client.post("/count/start", json={"session": "night"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})
    client.post("/api/scan", json={"raw": "1744-0100200-005"})
    assert client.post("/api/commit").get_json()["committed"] == 2
    again = client.post("/api/commit")
    assert again.status_code == 200
    assert again.get_json()["already_saved"] is True


def test_a_scan_after_the_count_was_saved_answers_in_json(client):
    """Not an HTML error page — the page parses this and shows a message."""
    client.post("/count/start", json={"session": "night"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})
    client.post("/api/scan", json={"raw": "1744-0100200-005"})
    client.post("/api/commit")

    r = client.post("/api/scan", json={"raw": "1780-0088010-002"})
    assert r.status_code == 409
    assert r.is_json
    body = r.get_json()
    assert body["restart"] is True and "already been saved" in body["error"]


def test_a_box_scanned_by_mistake_can_be_emptied_through_the_api(client):
    client.post("/count/start", json={"session": "night"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})     # wrong: box 1 is empty
    s = client.post("/api/clear", json={"slot": "A1"}).get_json()
    assert s["step"]["ok"] is True
    assert [b for b in s["slots"] if b["slot"] == "A1"][0]["scanned"] is False
    assert s["done"] == 0


def test_clearing_does_not_move_you_along(client):
    client.post("/count/start", json={"session": "night"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})
    s = client.post("/api/clear", json={"slot": "A1"}).get_json()
    assert s["current_slot"] == "A2"          # still where the walk had reached


def test_a_cleared_box_is_listed_as_still_needing_a_scan(client):
    client.post("/count/start", json={"session": "night"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})
    client.post("/api/clear", json={"slot": "A1"})
    s = client.post("/api/scan", json={"raw": "1744-0100200-005"}).get_json()
    assert s["walk_done"] is True
    assert s["pending"] == ["A1"]             # the guard will ask about it
