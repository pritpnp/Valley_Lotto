"""Multi-store: isolation between locations, and the role hierarchy.

The dangerous failure here is a *leak* — one store seeing or changing another's
inventory, staff or sales — so most of these assert separation rather than
features.
"""

import pytest
from sqlalchemy import select

from lottery_tracker.web.app import create_app
from lottery_tracker.web.models import Store, User, InventoryRow, AuditRow


@pytest.fixture()
def app(tmp_path):
    a = create_app({"DATABASE_URL": f"sqlite:///{tmp_path/'m.db'}", "SECRET_KEY": "k",
                    "DEFAULT_STORE": "hq", "SLOTS": "4", "REGISTER_CODE": None})
    a.config.update(TESTING=True)
    return a


@pytest.fixture()
def boss(app):
    """The superadmin, plus two stores each with a manager."""
    c = app.test_client()
    c.post("/register", data={"username": "boss", "password": "secret1"})
    c.post("/admin/stores", data={"action": "add", "name": "Valley Supermarket",
                                  "slots": "48", "timezone": "America/New_York"})
    c.post("/admin/stores", data={"action": "add", "name": "Valley Mart WB",
                                  "slots": "24", "timezone": "America/New_York"})
    c.post("/admin/users", data={"action": "add", "username": "sup_mgr",
                                 "password": "secret1", "store": "valley-supermarket"})
    c.post("/admin/users", data={"action": "add", "username": "wb_mgr",
                                 "password": "secret1", "store": "valley-mart-wb"})
    return c


def _login(app, username, pw="secret1"):
    c = app.test_client()
    c.post("/login", data={"username": username, "password": pw})
    return c


# --- roles ------------------------------------------------------------------

def test_first_account_becomes_superadmin(app):
    c = app.test_client()
    c.post("/register", data={"username": "boss", "password": "secret1"})
    with app.config["SESSION_FACTORY"]() as db:
        u = db.scalars(select(User)).first()
    assert u.role == "superadmin" and u.store is None
    assert c.get("/admin/stores").status_code == 200


def test_registration_closes_once_an_account_exists(app):
    c = app.test_client()
    c.post("/register", data={"username": "boss", "password": "secret1"})
    r = app.test_client().get("/register", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_manager_cannot_reach_chain_administration(app, boss):
    mgr = _login(app, "sup_mgr")
    assert mgr.get("/dashboard").status_code == 200      # their own store: fine
    assert mgr.get("/staff").status_code == 200          # their own people: fine
    for forbidden in ("/admin/stores", "/admin/users", "/overview"):
        assert mgr.get(forbidden).status_code == 403, forbidden


def test_manager_cannot_switch_to_another_store(app, boss):
    mgr = _login(app, "sup_mgr")
    r = mgr.post("/admin/act-as", data={"store": "valley-mart-wb"}, follow_redirects=False)
    assert r.status_code == 403
    # ...and is still pinned to their own store
    assert "Valley Supermarket" in mgr.get("/dashboard").data.decode()


# --- isolation --------------------------------------------------------------

def test_inventory_is_isolated_between_stores(app, boss):
    sup = _login(app, "sup_mgr")
    wb = _login(app, "wb_mgr")
    sup.post("/inventory/add", data={"game_number": "1750"})
    wb.post("/inventory/add", data={"game_number": "1744"})

    with app.config["SESSION_FACTORY"]() as db:
        rows = {(r.store, r.game_number) for r in db.scalars(select(InventoryRow)).all()}
    assert rows == {("valley-supermarket", "1750"), ("valley-mart-wb", "1744")}

    assert "1744" not in _carried(sup)
    assert "1750" not in _carried(wb)


def _carried(client):
    html = client.get("/inventory").data.decode()
    return html.split("Carried (")[1].split("Available to add")[0]


def test_staff_are_isolated_between_stores(app, boss):
    sup = _login(app, "sup_mgr")
    wb = _login(app, "wb_mgr")
    sup.post("/staff", data={"action": "add", "name": "Alex", "pin": "1234"})
    assert "Alex" in sup.get("/staff").data.decode().split("People (")[1]
    assert "Alex" not in wb.get("/staff").data.decode().split("People (")[1]
    # a PIN from one store must not open another
    assert b"Wrong PIN" in wb.post("/pin", data={"pin": "1234"}).data


def test_scans_land_in_the_signed_in_store(app, boss):
    sup = _login(app, "sup_mgr")
    sup.post("/count/start", json={"session": "morning"})
    sup.post("/api/scan", json={"raw": "1742011331200893"})
    sup.post("/api/commit")

    from lottery_tracker.web.models import ScanRow
    with app.config["SESSION_FACTORY"]() as db:
        stores = {r.store for r in db.scalars(select(ScanRow)).all()}
    assert stores == {"valley-supermarket"}
    assert _login(app, "wb_mgr").get("/report").data.decode().count("1742") == 0


# --- store profile & superadmin ---------------------------------------------

def test_store_profile_fields_are_saved(app, boss):
    boss.post("/admin/stores", data={
        "action": "update", "slug": "valley-mart-wb", "name": "Valley Mart WB",
        "slots": "24", "timezone": "America/New_York", "address": "1 Main St",
        "phone": "570-555-0100", "retailer_number": "PA-12345", "active": "on"})
    with app.config["SESSION_FACTORY"]() as db:
        st = db.get(Store, "valley-mart-wb")
    assert (st.address, st.phone, st.retailer_number, st.slots) == \
           ("1 Main St", "570-555-0100", "PA-12345", "24")


def test_each_store_gets_its_own_box_count(app, boss):
    boss.post("/admin/act-as", data={"store": "valley-mart-wb"})
    assert boss.post("/count/start", json={"session": "morning"}).get_json()["total"] == 24
    boss.post("/admin/act-as", data={"store": "valley-supermarket"})
    assert boss.post("/count/start", json={"session": "morning"}).get_json()["total"] == 48


def test_header_shows_the_store_name_for_store_people(app, boss):
    mgr = _login(app, "sup_mgr")
    assert "Valley Supermarket Lottery" in mgr.get("/dashboard").data.decode()
    # the superadmin sees the chain brand instead
    assert "Valley Lotto" in boss.get("/overview").data.decode()


def test_superadmin_can_act_as_any_store(app, boss):
    boss.post("/admin/act-as", data={"store": "valley-mart-wb"})
    assert "Valley Mart WB" in boss.get("/dashboard").data.decode()
    assert boss.get("/overview").status_code == 200


def test_actions_are_written_to_the_audit_trail(app, boss):
    sup = _login(app, "sup_mgr")
    sup.post("/inventory/add", data={"game_number": "1750"})
    with app.config["SESSION_FACTORY"]() as db:
        rows = db.scalars(select(AuditRow).where(AuditRow.action == "inventory.add")).all()
    assert rows and rows[0].store == "valley-supermarket"
    assert rows[0].actor == "sup_mgr"
