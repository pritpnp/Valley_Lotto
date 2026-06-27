import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lottery_app import db  # noqa: E402
from lottery_app.auth import hash_password, verify_password  # noqa: E402
from lottery_tracker.model import Game  # noqa: E402


# --- password hashing -------------------------------------------------------
def test_hash_roundtrip():
    h = hash_password("hunter2")
    assert h.startswith("scrypt$")
    assert verify_password("hunter2", h)
    assert not verify_password("hunter3", h)


def test_hash_is_salted():
    assert hash_password("same") != hash_password("same")  # random salt each time


def test_verify_rejects_garbage():
    assert not verify_password("x", "not-a-hash")
    assert not verify_password("x", "bcrypt$stuff")


def test_empty_password_rejected():
    with pytest.raises(ValueError):
        hash_password("")


# --- db isolation between stores --------------------------------------------
def test_inventory_isolated_per_store(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    db.init_db(conn)
    a = db.create_store(conn, "a", "Store A")
    b = db.create_store(conn, "b", "Store B")
    db.add_inventory(conn, a, "1736")
    db.add_inventory(conn, a, "1744")
    db.add_inventory(conn, b, "1778")
    assert db.get_inventory(conn, a) == {"1736", "1744"}
    assert db.get_inventory(conn, b) == {"1778"}
    db.remove_inventory(conn, a, "1736")
    assert db.get_inventory(conn, a) == {"1744"}


def test_users_unique_per_store_but_reusable_across(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    db.init_db(conn)
    a = db.create_store(conn, "a", "A")
    b = db.create_store(conn, "b", "B")
    db.create_user(conn, a, "manager", hash_password("p1"), "owner")
    # same username in another store is fine
    db.create_user(conn, b, "manager", hash_password("p2"), "owner")
    assert db.find_user(conn, a, "manager")["store_id"] == a
    assert db.find_user(conn, b, "manager")["store_id"] == b


# --- end-to-end via TestClient ----------------------------------------------
def _client(tmp_path, monkeypatch):
    # Build a tiny shared catalog the app can read.
    state = {
        "captured_at": "2026-06-27T16:00:00Z",
        "games": {
            "1736": {"game_number": "1736", "name": "HIGH 5", "price": 5, "status": "active",
                     "odds": "1:4.2",
                     "prize_tiers": [{"value": "$100000", "remaining": 1},
                                     {"value": "$50", "remaining": 500}],
                     "tier_originals": {"100000.0": 2, "50.0": 1000}},
            "1778": {"game_number": "1778", "name": "MONEY RUSH", "price": 2, "status": "active",
                     "odds": "1:3.5",
                     "prize_tiers": [{"value": "$30000", "remaining": 4},
                                     {"value": "$20", "remaining": 800}],
                     "tier_originals": {"30000.0": 5, "20.0": 1000}},
        },
    }
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state))
    db_path = tmp_path / "app.db"

    monkeypatch.setenv("LOTTO_DB", str(db_path))
    monkeypatch.setenv("LOTTO_STATE", str(state_path))
    monkeypatch.setenv("LOTTO_SECRET", "test-secret")

    # Import the module fresh so it picks up the env vars.
    for mod in [m for m in list(sys.modules) if m.startswith("lottery_app.main")]:
        del sys.modules[mod]
    from lottery_app import main  # noqa: WPS433

    conn = db.connect(db_path)
    db.init_db(conn)
    sid = db.create_store(conn, "main", "Main St")
    db.create_user(conn, sid, "owner", hash_password("secret"), "admin")
    db.add_inventory(conn, sid, "1736")
    conn.close()

    from fastapi.testclient import TestClient

    return TestClient(main.app), sid


def test_login_required(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_bad_login_rejected(tmp_path, monkeypatch):
    client, sid = _client(tmp_path, monkeypatch)
    r = client.post("/login", data={"store_id": sid, "username": "owner", "password": "wrong"},
                    follow_redirects=False)
    assert r.status_code == 303 and "error" in r.headers["location"]


def test_login_and_dashboard(tmp_path, monkeypatch):
    client, sid = _client(tmp_path, monkeypatch)
    r = client.post("/login", data={"store_id": sid, "username": "owner", "password": "secret"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "HIGH 5" in page.text          # carried game shows up
    assert "MONEY RUSH" in page.text      # appears in the bring-in board (not carried)


def test_swap_targets_same_price_keepworthy():
    from lottery_tracker.rules import RatingWeights, Thresholds
    from lottery_app.pa_data import Catalog, swap_targets
    games = {
        "carried5": Game(game_number="carried5", price=5, status="active", odds="1:4.9",
                         prize_tiers=[{"value": "$5", "remaining": 1}],
                         tier_originals={"5.0": 100}),                 # bad, carried
        "fresh5": Game(game_number="fresh5", price=5, status="active", odds="1:3.1",
                       prize_tiers=[{"value": "$100", "remaining": 9}, {"value": "$5", "remaining": 9000}],
                       tier_originals={"100.0": 10, "5.0": 10000}),    # great $5, not carried
        "stale5": Game(game_number="stale5", price=5, status="active", odds="1:4.9",
                       prize_tiers=[{"value": "$5", "remaining": 100}],
                       tier_originals={"5.0": 10000}),                 # picked-over $5
        "fresh10": Game(game_number="fresh10", price=10, status="active", odds="1:3.0",
                        prize_tiers=[{"value": "$10", "remaining": 9000}],
                        tier_originals={"10.0": 10000}),               # wrong price
    }
    cat = Catalog(games=games)
    out = swap_targets(cat, {"carried5"}, 5.0, Thresholds(), RatingWeights())
    nums = [r["game_number"] for r in out]
    assert "fresh5" in nums            # the strong same-price option is offered
    assert "stale5" not in nums        # picked-over (below cutoff) is not
    assert "fresh10" not in nums       # wrong price excluded


def test_inventory_add_remove_flow(tmp_path, monkeypatch):
    client, sid = _client(tmp_path, monkeypatch)
    client.post("/login", data={"store_id": sid, "username": "owner", "password": "secret"})
    client.post("/inventory/add", data={"game_number": "1778"})
    assert db.get_inventory(db.connect(tmp_path / "app.db"), sid) == {"1736", "1778"}
    client.post("/inventory/remove", data={"game_number": "1736"})
    assert db.get_inventory(db.connect(tmp_path / "app.db"), sid) == {"1778"}
