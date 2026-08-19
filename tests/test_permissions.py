"""Per-person capabilities: a slice of manager work without the whole role."""

import pytest
from sqlalchemy import select

from lottery_tracker.web import permissions as perms
from lottery_tracker.web.app import create_app
from lottery_tracker.web.models import StaffRow


# --- the permission strings themselves ---------------------------------------

def test_counting_is_always_held_and_never_stored():
    assert "count" in perms.parse("")
    assert "count" in perms.parse(None)
    assert perms.dump({"count"}) == ""            # implicit, not persisted


def test_unknown_keys_are_dropped_not_trusted():
    """A capability deleted from the code must stop granting anything."""
    assert perms.parse("reports,launch_missiles") == {"count", "reports"}
    assert perms.dump({"reports", "launch_missiles"}) == "reports"


def test_dump_is_canonical_order_so_rows_compare_equal():
    assert perms.dump({"settle", "reports"}) == perms.dump({"reports", "settle"})


def test_describe_reads_like_a_sentence():
    assert perms.describe("") == "Counts only"
    assert perms.describe("reports,receive") == "Counts + see daily sales, receive shipments"


# --- the web app --------------------------------------------------------------

@pytest.fixture()
def app(tmp_path):
    a = create_app({"DATABASE_URL": f"sqlite:///{tmp_path/'p.db'}", "SECRET_KEY": "k",
                    "DEFAULT_STORE": "t", "SLOTS": "2", "REGISTER_CODE": None})
    a.config.update(TESTING=True)
    return a


@pytest.fixture()
def boss(app):
    c = app.test_client()
    c.post("/register", data={"username": "prit", "password": "pw"})
    return c


def _add(client, name, pin, role="employee", perm=()):
    return client.post("/staff", data={"action": "add", "name": name, "pin": pin,
                                       "role": role, "perm": list(perm)},
                       follow_redirects=True)


def _pin_in(client, name, pin, app):
    with app.app_context():
        pass
    return client.post("/pin", data={"pin": pin}, follow_redirects=False)


def test_an_employee_with_nothing_ticked_can_only_count(boss, app):
    _add(boss, "Sam", "1111")
    _pin_in(boss, "Sam", "1111", app)
    assert boss.get("/count").status_code == 200
    for denied in ("/report", "/history", "/staff", "/access", "/weights"):
        assert boss.get(denied).status_code == 403, denied


def test_ticking_one_box_grants_exactly_that(boss, app):
    _add(boss, "Dana", "2222", perm=["reports"])
    _pin_in(boss, "Dana", "2222", app)
    assert boss.get("/report").status_code == 200
    assert boss.get("/history").status_code == 403
    assert boss.get("/staff").status_code == 403


def test_a_staff_manager_holds_everything(boss, app):
    _add(boss, "Ravi", "3333", role="manager")
    _pin_in(boss, "Ravi", "3333", app)
    for page in ("/report", "/history", "/staff", "/access", "/weights"):
        assert boss.get(page).status_code == 200, page


def test_grants_can_be_changed_after_the_fact(boss, app):
    _add(boss, "Kim", "4444")
    with app.app_context():
        pass
    sid = None
    with app.test_request_context():
        pass
    # find the row through the app's own session factory
    Session = app.config["SESSION_FACTORY"]
    with Session() as db:
        sid = db.scalar(select(StaffRow).where(StaffRow.name == "Kim")).id

    boss.post("/staff", data={"action": "permissions", "staff_id": sid,
                              "role": "employee", "perm": ["backstock", "settle"]},
              follow_redirects=True)
    with Session() as db:
        row = db.scalar(select(StaffRow).where(StaffRow.name == "Kim"))
        assert row.granted() == {"count", "backstock", "settle"}


def test_changing_your_own_grants_takes_effect_immediately(boss, app):
    """Not at next sign-in — the session carries the grants, so it must refresh."""
    _add(boss, "Lee", "5555", perm=["staff"])
    Session = app.config["SESSION_FACTORY"]
    with Session() as db:
        sid = db.scalar(select(StaffRow).where(StaffRow.name == "Lee")).id
    _pin_in(boss, "Lee", "5555", app)
    assert boss.get("/report").status_code == 403

    boss.post("/staff", data={"action": "permissions", "staff_id": sid,
                              "role": "employee", "perm": ["staff", "reports"]},
              follow_redirects=True)
    assert boss.get("/report").status_code == 200


def test_the_menu_only_offers_what_the_person_can_reach(boss, app):
    _add(boss, "Nia", "6666", perm=["reports"])
    _pin_in(boss, "Nia", "6666", app)
    html = boss.get("/count").data.decode()
    assert "/report" in html
    assert "/staff" not in html and "/access" not in html


def test_a_manager_account_still_sees_everything(boss):
    """The signed-in manager (no PIN in play) is unchanged by all this."""
    for page in ("/report", "/history", "/staff", "/access", "/weights"):
        assert boss.get(page).status_code == 200, page
