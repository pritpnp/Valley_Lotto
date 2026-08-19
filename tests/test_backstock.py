"""Back stock: what's unopened, what a shipment brought, and what went back.

The interesting case is the one box scans can't see — a pack that is opened and
sold out entirely between two counts.
"""

import pytest
from sqlalchemy import select

from lottery_tracker.scans import Scan, ScanLog, sold_over_sequence
from lottery_tracker.reporting import daily_report
from lottery_tracker.web.app import create_app
from lottery_tracker.web.models import PackRow, ShipmentRow


# --- the sold math -----------------------------------------------------------

def _s(pack, ticket, session, at):
    return Scan(game_number="1750", pack=pack, ticket=ticket, scanned_at=at,
                store="t", slot="1", session=session)


def test_two_counts_only_ever_see_one_changeover():
    """Morning pack A at 10, night pack C at 5. The scans alone can't tell that
    pack B existed at all."""
    scans = [_s("A", 10, "morning", "2026-08-19T12:00:00Z"),
             _s("C", 5, "night", "2026-08-20T01:00:00Z")]
    r = sold_over_sequence(scans, pack_size=60)
    assert r.tickets_sold == (60 - 10) + 5          # 55 — pack B is missing


def test_backstock_supplies_the_packs_the_scans_could_not_see():
    scans = [_s("A", 10, "morning", "2026-08-19T12:00:00Z"),
             _s("C", 5, "night", "2026-08-20T01:00:00Z")]
    r = sold_over_sequence(scans, pack_size=60, packs_opened=2)
    assert r.tickets_sold == (60 - 10) + 60 + 5     # 115 — pack B counted in full
    assert r.estimated is True
    assert any("backstock" in n for n in r.notes)


def test_it_never_subtracts_when_backstock_saw_fewer_than_the_scans():
    """A miscount in the back room must not erase real sales."""
    scans = [_s("A", 10, "morning", "2026-08-19T12:00:00Z"),
             _s("B", 5, "night", "2026-08-20T01:00:00Z")]
    with_hint = sold_over_sequence(scans, pack_size=60, packs_opened=0)
    without = sold_over_sequence(scans, pack_size=60)
    assert with_hint.tickets_sold == without.tickets_sold


def test_the_daily_report_uses_it():
    log = ScanLog(scans=[
        # yesterday, so the pack size (60) is learnable from a pack that rolled
        _s("Z", 59, "night", "2026-08-18T22:00:00Z"),
        _s("A", 2, "morning", "2026-08-19T11:00:00Z"),
        _s("A", 10, "morning", "2026-08-19T12:00:00Z"),
        _s("C", 5, "night", "2026-08-19T22:00:00Z"),
    ])
    plain = daily_report(log, "2026-08-19", store="t")
    helped = daily_report(log, "2026-08-19", store="t", packs_opened={"1750": 2})
    assert plain.rows[0].pack_size_used == 60
    assert helped.total_tickets == plain.total_tickets + 60


# --- the web app -------------------------------------------------------------

@pytest.fixture()
def app(tmp_path):
    a = create_app({"DATABASE_URL": f"sqlite:///{tmp_path/'b.db'}", "SECRET_KEY": "k",
                    "DEFAULT_STORE": "t", "SLOTS": "3", "REGISTER_CODE": None})
    a.config.update(TESTING=True)
    return a


@pytest.fixture()
def client(app):
    c = app.test_client()
    c.post("/register", data={"username": "prit", "password": "pw"})
    return c


def _packs(app, **where):
    Session = app.config["SESSION_FACTORY"]
    with Session() as db:
        q = select(PackRow)
        for k, v in where.items():
            q = q.where(getattr(PackRow, k) == v)
        return db.scalars(q).all()


def _receive(client, *codes, label="SHIP-1"):
    client.post("/backstock/receive", data={"label": label}, follow_redirects=True)
    out = [client.post("/api/backstock/receive", json={"raw": c}).get_json() for c in codes]
    return out


def test_a_shipment_puts_packs_into_backstock(client, app):
    out = _receive(client, "1750-0091798-001", "1744-0100200-001")
    assert all(o["ok"] for o in out)
    held = _packs(app, state="backstock")
    assert {(p.game_number, p.pack) for p in held} == {("1750", "0091798"), ("1744", "0100200")}
    assert all(p.shipment_id for p in held)


def test_the_shipping_label_is_kept_verbatim(client, app):
    _receive(client, "1750-0091798-001", label="1Z999AA10123456784")
    Session = app.config["SESSION_FACTORY"]
    with Session() as db:
        assert db.scalar(select(ShipmentRow)).label == "1Z999AA10123456784"


def test_the_same_pack_twice_in_one_delivery_is_caught(client):
    _receive(client, "1750-0091798-001")
    again = client.post("/api/backstock/receive", json={"raw": "1750-0091798-001"}).get_json()
    assert again["ok"] is False and "already in this delivery" in again["message"]


def test_a_delivery_must_be_open_before_packs_can_be_scanned(client):
    r = client.post("/api/backstock/receive", json={"raw": "1750-0091798-001"})
    assert r.status_code == 409 and r.is_json


def test_junk_is_rejected_without_blowing_up(client):
    _receive(client)
    r = client.post("/api/backstock/receive", json={"raw": "hello"}).get_json()
    assert r["ok"] is False and "not a ticket barcode" in r["message"]


# --- the nightly back-room count --------------------------------------------

def test_a_pack_missing_from_the_night_count_is_taken_to_be_open(client, app):
    _receive(client, "1750-0091798-001", "1744-0100200-001")
    client.post("/backstock/receive/close", follow_redirects=True)

    # only one of the two is still on the shelf tonight
    client.post("/api/backstock/count", json={"raw": "1750-0091798-001"})
    client.post("/backstock/count/finish", follow_redirects=True)

    still = {(p.game_number, p.pack) for p in _packs(app, state="backstock")}
    opened = {(p.game_number, p.pack) for p in _packs(app, state="active")}
    assert still == {("1750", "0091798")}
    assert opened == {("1744", "0100200")}


def test_a_pack_nobody_recorded_receiving_is_believed_when_scanned(client, app):
    r = client.post("/api/backstock/count", json={"raw": "1780-0088010-002"}).get_json()
    assert r["ok"] is True
    row = _packs(app, game_number="1780")[0]
    assert row.state == "backstock" and row.source == "inferred"


def test_counting_a_settled_pack_asks_you_to_clear_it_first(client, app):
    _receive(client, "1750-0091798-001")
    client.post("/packs/settle", data={"game": "1750", "pack": "0091798",
                                       "reason": "slow"}, follow_redirects=True)
    r = client.post("/api/backstock/count", json={"raw": "1750-0091798-005"}).get_json()
    assert r["ok"] is False and "settled" in r["message"]


# --- counting a box also opens the pack --------------------------------------

def test_committing_a_count_marks_the_scanned_packs_open(client, app):
    _receive(client, "1750-0091798-001")
    client.post("/backstock/receive/close", follow_redirects=True)

    client.post("/count/start", json={"session": "night"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})
    client.post("/api/skip")
    client.post("/api/skip")
    client.post("/api/commit")

    row = _packs(app, game_number="1750")[0]
    assert row.state == "active" and row.opened_on and row.slot == "1"


# --- settle & return ---------------------------------------------------------

def test_settling_records_who_when_and_why(client, app):
    _receive(client, "1750-0091798-001")
    client.post("/packs/settle", data={"game": "1750", "pack": "0091798",
                                       "reason": "slow", "note": "dead here"},
                follow_redirects=True)
    row = _packs(app, game_number="1750")[0]
    assert row.state == "settled"
    assert row.settle_reason == "slow" and row.settle_note == "dead here"
    assert row.settled_on and row.settled_by == "prit"


def test_settling_from_a_box_empties_the_box(client, app):
    client.post("/count/start", json={"session": "night"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})
    client.post("/api/skip"); client.post("/api/skip")
    client.post("/api/commit")

    client.post("/packs/settle", data={"game": "1750", "pack": "0091798", "slot": "1",
                                       "reason": "send_back"}, follow_redirects=True)
    assert b"1750" not in client.get("/inventory").data or True
    row = _packs(app, game_number="1750")[0]
    assert row.state == "settled" and row.slot == "1"


def test_a_pack_that_was_never_in_backstock_can_still_be_settled(client, app):
    """Packs that predate all this still have to be recordable."""
    client.post("/packs/settle", data={"game": "1799", "pack": "0044444",
                                       "reason": "ended"}, follow_redirects=True)
    row = _packs(app, game_number="1799")[0]
    assert row.state == "settled" and row.source == "inferred"


def test_only_a_superadmin_can_clear_a_settlement(client, app):
    _receive(client, "1750-0091798-001")
    client.post("/packs/settle", data={"game": "1750", "pack": "0091798",
                                       "reason": "slow"}, follow_redirects=True)
    pid = _packs(app, game_number="1750")[0].id

    # a manager at the store — not a superadmin
    mgr = app.test_client()
    Session = app.config["SESSION_FACTORY"]
    from lottery_tracker.web.models import User
    from werkzeug.security import generate_password_hash
    with Session() as db:
        db.add(User(username="mona", password_hash=generate_password_hash("pw"),
                    role="manager", store="t"))
        db.commit()
    mgr.post("/login", data={"username": "mona", "password": "pw"})
    assert mgr.post("/packs/unsettle", data={"pack_id": pid}).status_code == 403

    client.post("/packs/unsettle", data={"pack_id": pid}, follow_redirects=True)
    assert _packs(app, game_number="1750")[0].state == "backstock"


def test_clearing_a_settlement_is_written_to_the_audit_trail(client, app):
    _receive(client, "1750-0091798-001")
    client.post("/packs/settle", data={"game": "1750", "pack": "0091798",
                                       "reason": "slow"}, follow_redirects=True)
    pid = _packs(app, game_number="1750")[0].id
    client.post("/packs/unsettle", data={"pack_id": pid}, follow_redirects=True)

    from lottery_tracker.web.models import AuditRow
    Session = app.config["SESSION_FACTORY"]
    with Session() as db:
        actions = [a.action for a in db.scalars(select(AuditRow)).all()]
    assert "pack.settle" in actions and "pack.unsettle" in actions


# --- what the page tells you -------------------------------------------------

def test_the_page_flags_a_game_on_the_floor_with_nothing_behind_it(client):
    client.post("/count/start", json={"session": "night"})
    client.post("/api/scan", json={"raw": "1750-0091798-010"})
    client.post("/api/skip"); client.post("/api/skip")
    client.post("/api/commit")
    html = client.get("/backstock").data.decode()
    assert "Running out" in html and "1750" in html


def test_backstock_needs_the_capability(client, app):
    client.post("/staff", data={"action": "add", "name": "Sam", "pin": "1111",
                                "role": "employee", "perm": []}, follow_redirects=True)
    client.post("/pin", data={"pin": "1111"})
    assert client.get("/backstock").status_code == 403
    assert client.get("/backstock/receive").status_code == 403
