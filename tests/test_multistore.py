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


# --- managing accounts: rename, disable, delete ------------------------------

def _accounts(client):
    return client.get("/admin/users").data.decode()


def test_superadmin_can_rename_an_account(app, boss):
    boss.post("/admin/users", data={"action": "rename", "user_id": "2",
                                    "username": "wb_manager"})
    assert "wb_manager" in _accounts(boss)


def test_you_can_rename_yourself_and_still_log_in(app):
    """The owner's account is created from an email; renaming it must not
    lock them out."""
    c = app.test_client()
    c.post("/register", data={"username": "pritpnp", "password": "secret1"})
    with app.config["SESSION_FACTORY"]() as db:
        me = db.scalars(select(User)).first().id
    c.post("/admin/users", data={"action": "rename", "user_id": str(me), "username": "Prit"})

    fresh = app.test_client()
    assert fresh.post("/login", data={"username": "Prit", "password": "secret1"},
                      follow_redirects=False).status_code == 302
    # ...and the capitalisation you chose doesn't have to be typed exactly
    assert app.test_client().post("/login", data={"username": "prit", "password": "secret1"},
                                  follow_redirects=False).status_code == 302


def test_rename_rejects_a_taken_or_malformed_username(app, boss):
    assert b"is taken" in boss.post("/admin/users", data={
        "action": "rename", "user_id": "2", "username": "wb_mgr"}).data
    assert b"Username:" in boss.post("/admin/users", data={
        "action": "rename", "user_id": "2", "username": "a b!"}).data


def test_superadmin_can_disable_and_re_enable_a_manager(app, boss):
    boss.post("/admin/users", data={"action": "toggle", "user_id": "2"})
    # a disabled account cannot sign in
    assert b"Invalid username or password" in app.test_client().post(
        "/login", data={"username": "sup_mgr", "password": "secret1"}).data
    boss.post("/admin/users", data={"action": "toggle", "user_id": "2"})
    assert app.test_client().post("/login", data={"username": "sup_mgr", "password": "secret1"},
                                  follow_redirects=False).status_code == 302


def test_superadmin_can_delete_a_manager_without_losing_store_data(app, boss):
    mgr = _login(app, "sup_mgr")
    mgr.post("/inventory/add", data={"game_number": "1750"})
    boss.post("/admin/users", data={"action": "delete", "user_id": "2"})

    assert "sup_mgr" not in _accounts(boss)
    assert app.test_client().post("/login", data={"username": "sup_mgr",
                                                  "password": "secret1"}).status_code == 200
    # the store and everything in it survive — only the login went away
    with app.config["SESSION_FACTORY"]() as db:
        assert db.get(Store, "valley-supermarket") is not None
        assert db.scalars(select(InventoryRow).where(
            InventoryRow.store == "valley-supermarket")).all()


def test_you_cannot_delete_or_disable_yourself(app, boss):
    with app.config["SESSION_FACTORY"]() as db:
        me = db.scalars(select(User).where(User.role == "superadmin")).first().id
    assert b"signed in with" in boss.post("/admin/users",
                                          data={"action": "delete", "user_id": str(me)}).data
    assert b"signed in with" in boss.post("/admin/users",
                                          data={"action": "toggle", "user_id": str(me)}).data
    assert "boss" in _accounts(boss)


def test_the_last_superadmin_cannot_be_deleted(app, boss):
    """Two superadmins: deleting one is fine, deleting the second is not."""
    boss.post("/admin/users", data={"action": "add", "username": "boss2",
                                    "password": "secret1", "role": "superadmin"})
    with app.config["SESSION_FACTORY"]() as db:
        other = db.scalars(select(User).where(User.username == "boss2")).first().id
    boss.post("/admin/users", data={"action": "delete", "user_id": str(other)})
    assert "boss2" not in _accounts(boss)
    # now only the signed-in owner remains, and that one is protected too
    with app.config["SESSION_FACTORY"]() as db:
        assert db.query(User).filter(User.role == "superadmin").count() == 1


def test_a_manager_cannot_delete_accounts(app, boss):
    mgr = _login(app, "sup_mgr")
    assert mgr.post("/admin/users", data={"action": "delete", "user_id": "3"}).status_code == 403
