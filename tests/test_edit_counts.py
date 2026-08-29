"""Correcting a count after the fact, and typing up one done on paper."""

import pytest
from sqlalchemy import select

from lottery_tracker.session import CountSession
from lottery_tracker.web.app import create_app
from lottery_tracker.web.models import ScanRow, BoxRow, AuditRow


# --- setting a box by hand, mid-count ---------------------------------------

def test_a_box_can_be_set_by_hand_without_a_barcode():
    sess = CountSession(slots=["A1", "A2"])
    step = sess.set_entry("A1", game_number="1750", pack="0091798", ticket=42)
    assert step.ok
    e = sess.entries["A1"]
    assert (e.game_number, e.pack, e.ticket) == ("1750", "0091798", 42)
    assert sess.current_slot == "A1"          # setting a box doesn't move you


def test_hand_entry_refuses_nonsense():
    sess = CountSession(slots=["A1"])
    assert sess.set_entry("A1", game_number="", pack="x", ticket=1).ok is False
    assert sess.set_entry("A1", game_number="1750", pack="", ticket="abc").ok is False
    assert sess.set_entry("A1", game_number="1750", pack="", ticket=-2).ok is False
    assert sess.set_entry("ZZ", game_number="1750", pack="", ticket=1).ok is False


def test_hand_entry_can_correct_a_scan():
    sess = CountSession(slots=["A1", "A2"])
    sess.scan("1750-0091798-010", at="t1")
    sess.set_entry("A1", game_number="1750", pack="0091798", ticket=107)
    assert sess.entries["A1"].ticket == 107


# --- the web app --------------------------------------------------------------

@pytest.fixture()
def app(tmp_path):
    a = create_app({"DATABASE_URL": f"sqlite:///{tmp_path/'e.db'}", "SECRET_KEY": "k",
                    "DEFAULT_STORE": "t", "SLOTS": "3", "REGISTER_CODE": None})
    a.config.update(TESTING=True)
    return a


@pytest.fixture()
def client(app):
    c = app.test_client()
    c.post("/register", data={"username": "prit", "password": "pw"})
    return c


def _count(client, session, *codes):
    client.post("/count/start", json={"session": session})
    for code in codes:
        client.post("/api/scan", json={"raw": code})
    for _ in range(3):
        client.post("/api/skip")
    client.post("/api/commit")


def _rows(app, **where):
    with app.config["SESSION_FACTORY"]() as db:
        q = select(ScanRow)
        for k, v in where.items():
            q = q.where(getattr(ScanRow, k) == v)
        return db.scalars(q).all()


def _today(client):
    return client.post("/api/commit").get_json().get("date")


def test_a_ticket_number_can_be_fixed_mid_count(client, app):
    client.post("/count/start", json={"session": "night"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})
    s = client.post("/api/edit", json={"slot": "1", "game": "1750",
                                       "pack": "0091798", "ticket": "107"}).get_json()
    assert s["step"]["ok"] is True
    box = next(b for b in s["slots"] if b["slot"] == "1")
    assert box["ticket"] == 107
    assert s["current_slot"] == "2"          # your place is kept


def test_the_history_page_lists_every_day_with_its_counts(client):
    _count(client, "morning", "1750-0091798-010")
    html = client.get("/history?tab=sales").data.decode()
    assert "Day by day" in html
    assert "Edit count" in html               # the morning count can be corrected
    assert "Type it in" in html               # the night count can be entered


def test_todays_row_is_there_before_anyone_counts(client):
    html = client.get("/history?tab=sales").data.decode()
    assert "today" in html and "no counts yet" in html


def test_editing_a_box_of_a_saved_count(client, app):
    _count(client, "night", "1750-0091798-010")
    date = _rows(app)[0].scanned_at[:10]

    page = client.get(f"/counts/{date}/night/edit")
    assert page.status_code == 200
    assert b"1750" in page.data

    client.post(f"/counts/{date}/night/box/1",
                data={"game_number": "1750", "pack": "0091798", "ticket": "107"},
                follow_redirects=True)
    row = _rows(app, slot="1")[0]
    assert row.ticket == 107


def test_a_box_can_be_taken_out_of_a_saved_count(client, app):
    _count(client, "night", "1750-0091798-010")
    date = _rows(app)[0].scanned_at[:10]
    client.post(f"/counts/{date}/night/box/1", data={"action": "delete"},
                follow_redirects=True)
    assert _rows(app, slot="1") == []


def test_a_box_missed_by_the_count_can_be_added_afterwards(client, app):
    _count(client, "night", "1750-0091798-010")
    date = _rows(app)[0].scanned_at[:10]
    client.post(f"/counts/{date}/night/box/2",
                data={"game_number": "1744", "pack": "0100200", "ticket": "5"},
                follow_redirects=True)
    row = _rows(app, slot="2")[0]
    assert (row.game_number, row.ticket) == ("1744", 5)


def test_a_scan_fills_the_three_fields(client, app):
    _count(client, "night", "1750-0091798-010")
    date = _rows(app)[0].scanned_at[:10]
    client.post(f"/counts/{date}/night/box/2", data={"scan": "1744-0100200-005"},
                follow_redirects=True)
    row = _rows(app, slot="2")[0]
    assert (row.game_number, row.pack, row.ticket) == ("1744", "0100200", 5)


def test_a_paper_count_starts_from_the_previous_one(client, app):
    _count(client, "morning", "1750-0091798-010", "1744-0100200-005")
    date = _rows(app)[0].scanned_at[:10]

    client.post(f"/counts/{date}/night/create", follow_redirects=True)
    night = [r for r in _rows(app) if r.session == "night"]
    assert {r.slot for r in night} == {"1", "2"}
    assert {(r.game_number, r.pack) for r in night} == {("1750", "0091798"), ("1744", "0100200")}


def test_typing_in_a_count_that_already_exists_just_opens_it(client, app):
    _count(client, "night", "1750-0091798-010")
    date = _rows(app)[0].scanned_at[:10]
    before = len(_rows(app))
    client.post(f"/counts/{date}/night/create", follow_redirects=True)
    assert len(_rows(app)) == before        # nothing duplicated


def test_correcting_the_newest_count_moves_the_box_map(client, app):
    _count(client, "night", "1750-0091798-010")
    date = _rows(app)[0].scanned_at[:10]
    client.post(f"/counts/{date}/night/box/1",
                data={"game_number": "1744", "pack": "0100200", "ticket": "5"},
                follow_redirects=True)
    with app.config["SESSION_FACTORY"]() as db:
        assert db.scalar(select(BoxRow).where(BoxRow.slot == "1")).game_number == "1744"


def test_correcting_an_older_count_leaves_the_box_map_alone(client, app):
    """A correction to last week must not overwrite a box that changed since."""
    _count(client, "morning", "1750-0091798-010")
    _count(client, "night", "1780-0088010-002")      # the box moved on
    date = _rows(app)[0].scanned_at[:10]

    client.post(f"/counts/{date}/morning/box/1",
                data={"game_number": "1744", "pack": "0100200", "ticket": "5"},
                follow_redirects=True)
    with app.config["SESSION_FACTORY"]() as db:
        assert db.scalar(select(BoxRow).where(BoxRow.slot == "1")).game_number == "1780"


def test_every_correction_is_written_to_the_change_log(client, app):
    _count(client, "night", "1750-0091798-010")
    date = _rows(app)[0].scanned_at[:10]
    client.post(f"/counts/{date}/night/box/1",
                data={"game_number": "1750", "pack": "0091798", "ticket": "107"},
                follow_redirects=True)
    with app.config["SESSION_FACTORY"]() as db:
        entries = [(a.action, a.detail) for a in db.scalars(select(AuditRow)).all()]
    edit = [d for a, d in entries if a == "count.box.edit"]
    assert edit and "ticket 010" in edit[0] and "ticket 107" in edit[0]


def test_correcting_counts_needs_the_capability(client, app):
    _count(client, "night", "1750-0091798-010")
    date = _rows(app)[0].scanned_at[:10]
    client.post("/staff", data={"action": "add", "name": "Sam", "pin": "1111",
                                "role": "employee", "perm": []}, follow_redirects=True)
    client.post("/pin", data={"pin": "1111"})

    assert client.get(f"/counts/{date}/night/edit").status_code == 403
    assert client.post(f"/counts/{date}/night/create").status_code == 403
    assert client.post(f"/counts/{date}/night/box/1",
                       data={"game_number": "1", "ticket": "1"}).status_code == 403
    # and the buttons aren't dangled in front of them
    assert b"Edit count" not in client.get("/history?tab=sales").data


def test_editing_a_ticket_by_hand_keeps_the_pack(client, app):
    """The box menu prefills from what the page knows; the pack was missing from
    that, so correcting a ticket number quietly erased the pack."""
    client.post("/count/start", json={"session": "night"})
    s = client.post("/api/scan", json={"raw": "1750-0091798-010"}).get_json()
    box = next(b for b in s["slots"] if b["slot"] == "1")
    assert box["pack"] == "0091798"          # the menu has something to prefill

    s = client.post("/api/edit", json={"slot": "1", "game": box["game"],
                                       "pack": box["pack"], "ticket": "107"}).get_json()
    box = next(b for b in s["slots"] if b["slot"] == "1")
    assert (box["game"], box["pack"], box["ticket"]) == ("1750", "0091798", 107)
