"""PA's weekly order list, as the app uses it.

Three things, each only once a list has been saved (no list = exactly as before):

  * real tickets per pack, one answer shared by every report in the app,
  * replacements and the bring-in board only offer games you can order,
  * a carried game PA no longer ships is flagged wherever it shows up.

The lists here are written straight into the tables the feed job fills; the
games and sizes are PA's real list as of 10/26/26
(tests/fixtures/pa/Scratch-Offs_Order_10-26-26.pdf).
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, text

from lottery_app import pa_data
from lottery_app.pa_data import Catalog
from lottery_tracker.config import Config
from lottery_tracker.model import Game
from lottery_tracker.packs import DEFAULT_PACK_SIZE_BY_PRICE
from lottery_tracker.rules import RatingWeights, Thresholds
from lottery_tracker.web import app as appmod
from lottery_tracker.web.app import create_app
from lottery_tracker.web.models import OrderGameRow, OrderListRow, ScanRow
from _games import selling, verified

# PA's "Scratch-Offs Available for Ordering", as of 10/26/26:
# game number, price, tickets per pack.
PA_LIST_10_26_26 = """
1806 1 100  1801 1 100  1795 1 100  1783 1 100  1779 1 100  1773 1 100  1767 1 100
1805 2 100  1800 2 100  1794 2 100  1789 2 100  1778 2 100
1788 3 100  1753 3 100  1740 3 100
1804 5 60   1799 5 60   1793 5 60   1792 5 60   1787 5 60   1782 5 60   1777 5 60
1770 5 60   1765 5 60   1739 5 60
1803 10 60  1798 10 60  1791 10 60  1786 10 60  1781 10 60  1775 10 60  1769 10 60
1764 10 60  1751 10 60  1747 10 60  1735 10 60  1724 10 60  1719 10 60
1797 20 30  1785 20 30  1774 20 30  1763 20 30  1746 20 30
1802 30 30  1790 30 30  1780 30 30  1768 30 30  1750 30 30  1742 30 30  1734 30 30
1723 30 30
1796 50 50  1784 50 50  1762 50 50  1717 50 50
"""


def _pa_list():
    nums = PA_LIST_10_26_26.split()
    return [(nums[i], float(nums[i + 1]), int(nums[i + 2])) for i in range(0, len(nums), 3)]


@pytest.fixture()
def app(tmp_path):
    a = create_app({"DATABASE_URL": f"sqlite:///{tmp_path/'o.db'}", "SECRET_KEY": "k",
                    "DEFAULT_STORE": "t", "SLOTS": "4", "REGISTER_CODE": None})
    a.config.update(TESTING=True)
    return a


@pytest.fixture()
def client(app):
    c = app.test_client()
    c.post("/register", data={"username": "prit", "password": "pw"})   # superadmin
    return c


def _save_list(app, as_of, games):
    """Save one week's list the way the feed job does. ``games``: (number,
    price, tickets per pack) tuples."""
    with app.config["SESSION_FACTORY"]() as db:
        db.add(OrderListRow(as_of=as_of, url=f"https://example.test/{as_of}.pdf",
                            pdf_sha256="0" * 64, pdf=b"%PDF", games=len(games)))
        for num, price, tix in games:
            db.add(OrderGameRow(as_of=as_of, game_number=num, price=price,
                                tickets_per_pack=tix, name=f"Game {num}"))
        db.commit()


# --- 1. real pack sizes -------------------------------------------------------

def test_the_built_in_sizes_are_pas_own():
    games = _pa_list()
    assert len(games) == 55
    for num, price, tix in games:
        assert DEFAULT_PACK_SIZE_BY_PRICE[price].size == tix, num
    assert DEFAULT_PACK_SIZE_BY_PRICE[40.0].size == 15           # no $40 games: untouched


def test_config_yaml_agrees_with_pas_list():
    r = Config.load("config.yaml").pack_resolver()
    for num, price, tix in _pa_list():
        info = r.size_for(price)
        assert info.size == tix and info.confidence == "config-price", num
    assert r.size_for(40.0).size == 15


def _resolver_seen(client, monkeypatch, path="/report"):
    """The pack-size resolver a page hands to daily_report."""
    seen = []
    real = appmod.daily_report

    def spy(log, date, **kw):
        seen.append(kw.get("resolver"))
        return real(log, date, **kw)
    monkeypatch.setattr(appmod, "daily_report", spy)
    assert client.get(path).status_code == 200
    assert seen and all(r is not None for r in seen)
    return seen[0]


def test_with_no_order_list_the_sizes_are_config_yamls(client, monkeypatch):
    r = _resolver_seen(client, monkeypatch)
    assert r.size_for(1.0, game_number="1806").size == 100
    assert r.size_for(1.0, game_number="1806").confidence == "config-price"
    assert r.size_for(30.0, game_number="1750").size == 30


def test_each_game_takes_its_size_from_its_newest_list(app, client, monkeypatch):
    # 1767 is only on the older list; 1806 is on both and changed size.
    _save_list(app, "2026-10-19", [("1767", 1.0, 100), ("1806", 1.0, 120)])
    _save_list(app, "2026-10-26", [("1806", 1.0, 150), ("1750", 30.0, 30)])
    r = _resolver_seen(client, monkeypatch)
    assert r.size_for(1.0, game_number="1806").size == 150     # newest list wins
    assert r.size_for(1.0, game_number="1767").size == 100     # dropped, size kept
    assert r.size_for(1.0, game_number="1773").size == 100     # not on any: by price


def test_config_yamls_per_game_size_beats_the_order_list(app, client, monkeypatch):
    _save_list(app, "2026-10-26", [("1806", 1.0, 150)])
    real_load = Config.load.__func__

    def load(cls, path):
        cfg = real_load(cls, path)
        cfg.pack_sizes_by_game = {1806: 77}          # YAML reads an int key
        return cfg
    monkeypatch.setattr(Config, "load", classmethod(load))
    r = _resolver_seen(client, monkeypatch)
    assert r.size_for(1.0, game_number="1806").size == 77


def test_a_typo_in_config_pack_sizes_does_not_take_the_report_down(app, client,
                                                                    monkeypatch):
    _save_list(app, "2026-10-26", [("1806", 1.0, 150)])
    real_load = Config.load.__func__

    def load(cls, path):
        cfg = real_load(cls, path)
        cfg.pack_sizes_by_game = {"1806": "lots"}
        return cfg
    monkeypatch.setattr(Config, "load", classmethod(load))
    r = _resolver_seen(client, monkeypatch)                   # the page still renders
    assert r.size_for(1.0, game_number="1806").size == 150   # PA's number stands
    assert r.size_for(5.0, game_number="1804").size == 60


def _yesterday(tz="America/New_York"):
    return (datetime.now(ZoneInfo(tz)).date() - timedelta(days=1))


def _utc(day, hour, minute=0, tz="America/New_York"):
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(tz))
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _changeover_day(app):
    """A $1 game (1806, CASH GRAB) whose box changed pack between the counts:
    morning on ticket 090 of one pack, night on ticket 010 of the next. With
    PA's 100-ticket pack that's 10 + 10 = 20 tickets."""
    day = _yesterday()
    with app.config["SESSION_FACTORY"]() as db:
        db.add(ScanRow(store="t", game_number="1806", pack="0400001", ticket=90,
                       slot="1", session="morning", scanned_at=_utc(day, 9)))
        db.add(ScanRow(store="t", game_number="1806", pack="0400002", ticket=10,
                       slot="1", session="night", scanned_at=_utc(day, 19, 30)))
        db.commit()
    return day.isoformat()


def test_every_report_in_the_app_adds_the_same_day_up_the_same(app, client, monkeypatch):
    """The day's report, history, the chain overview and the store's own sales
    (behind the dashboard) used to size packs differently, so one day could
    total differently on each. They now share one answer."""
    _save_list(app, "2026-10-26", _pa_list())
    day = _changeover_day(app)

    from flask import request
    calls = []
    real = appmod.daily_report

    def spy(log, date, **kw):
        rep = real(log, date, **kw)
        calls.append((request.path, date, kw.get("resolver"), rep.total_tickets))
        return rep
    monkeypatch.setattr(appmod, "daily_report", spy)

    # The dashboard first: its sales figures are then kept for ten minutes, so
    # the overview would otherwise have worked them out for it already.
    for path in ("/dashboard", f"/report?date={day}", "/history", "/overview"):
        assert client.get(path).status_code == 200, path

    on_day = [c for c in calls if c[1] == day]
    assert {c[0] for c in on_day} == {"/report", "/history", "/overview", "/dashboard"}
    assert all(c[2] is not None for c in calls)
    assert {c[3] for c in on_day} == {20}


def test_every_report_uses_the_games_own_size_from_the_list(app, client, monkeypatch):
    """Same day, but this week's list prints 150 tickets a pack for the $1 game,
    unlike config.yaml's 100 for $1. Every page must use PA's 150:
    (150 - 90) + 10 = 70 tickets."""
    _save_list(app, "2026-10-26", [("1806", 1.0, 150)])
    day = _changeover_day(app)
    totals = {}
    real = appmod.daily_report

    def spy(log, date, **kw):
        rep = real(log, date, **kw)
        if date == day:
            from flask import request
            totals.setdefault(request.path, set()).add(rep.total_tickets)
        return rep
    monkeypatch.setattr(appmod, "daily_report", spy)
    for path in ("/dashboard", f"/report?date={day}", "/history", "/overview"):
        assert client.get(path).status_code == 200, path
    assert totals == {"/report": {70}, "/history": {70}, "/overview": {70},
                      "/dashboard": {70}}


def test_the_same_day_agrees_before_any_order_list_too(app, client, monkeypatch):
    day = _changeover_day(app)
    totals = {}
    real = appmod.daily_report

    def spy(log, date, **kw):
        rep = real(log, date, **kw)
        if date == day:
            from flask import request
            totals[request.path] = rep.total_tickets
        return rep
    monkeypatch.setattr(appmod, "daily_report", spy)
    for path in ("/dashboard", f"/report?date={day}", "/history", "/overview"):
        client.get(path)
    # config.yaml's $1 size is PA's 100 as well
    assert totals == {"/report": 20, "/history": 20, "/overview": 20, "/dashboard": 20}


def test_a_count_on_a_packs_last_ticket_doesnt_add_a_ticket_to_the_pack(app, client,
                                                                         monkeypatch):
    """The morning count catches 1806's pack on its last ticket, 099 (PA prints
    100 a pack: 000-099), and the night count finds the next pack on 010. That's
    1 + 10 = 11 tickets on a 100-ticket pack, on every page, not 12 on a
    101-ticket pack."""
    _save_list(app, "2026-10-26", _pa_list())
    day = _yesterday()
    with app.config["SESSION_FACTORY"]() as db:
        db.add(ScanRow(store="t", game_number="1806", pack="0400001", ticket=99,
                       slot="1", session="morning", scanned_at=_utc(day, 9)))
        db.add(ScanRow(store="t", game_number="1806", pack="0400002", ticket=10,
                       slot="1", session="night", scanned_at=_utc(day, 19, 30)))
        db.commit()
    day = day.isoformat()
    seen = {}
    real = appmod.daily_report

    def spy(log, date, **kw):
        rep = real(log, date, **kw)
        if date == day:
            from flask import request
            row = [r for r in rep.rows if r.game_number == "1806"][0]
            seen.setdefault(request.path, set()).add((rep.total_tickets, row.pack_size_used))
        return rep
    monkeypatch.setattr(appmod, "daily_report", spy)
    for path in ("/dashboard", f"/report?date={day}", "/history", "/overview"):
        assert client.get(path).status_code == 200, path
    assert seen == {"/report": {(11, 100)}, "/history": {(11, 100)},
                    "/overview": {(11, 100)}, "/dashboard": {(11, 100)}}


def test_the_order_list_is_read_once_until_a_new_one_arrives(app, client):
    _save_list(app, "2026-10-19", [("1806", 1.0, 100)])
    engine = app.config["SESSION_FACTORY"].kw["bind"]
    full_reads = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        if "tickets_per_pack" in statement and statement.lstrip().upper().startswith("SELECT"):
            full_reads.append(statement)

    client.get("/dashboard")
    client.get("/dashboard")
    client.get("/catalog")
    assert len(full_reads) == 1            # read once, then only checked

    _save_list(app, "2026-10-26", [("1806", 1.0, 100), ("1750", 30.0, 30)])
    html = client.get("/catalog").data.decode()
    assert len(full_reads) == 2            # the new list is picked up at once
    assert "as of Oct 26" in html
    event.remove(engine, "before_cursor_execute", _count)


def test_pages_still_work_if_the_order_tables_cannot_be_read(app, client):
    engine = app.config["SESSION_FACTORY"].kw["bind"]
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE pa_order_games"))
    client.post("/inventory/add", data={"game_number": "1744"})
    for path in ("/dashboard", "/catalog", "/inventory", "/catalog/1744", "/report",
                 "/history", "/overview"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "no longer ships" not in r.data.decode()


# --- 2. only suggest games you can order --------------------------------------

TYPICAL = 50_000


def _poor(num, price, per_day=200):
    g = selling(verified(Game(game_number=num, name=f"Slow {num}", price=price,
                              status="active", odds="1:4.9",
                              prize_tiers=[{"value": f"${price}", "remaining": 2}],
                              tier_originals={f"{float(price)}": 100})), per_day)
    g.peer_typical_sales = TYPICAL
    return g


def _fresh(num, price, per_day):
    g = selling(verified(Game(game_number=num, name=f"Fresh {num}", price=price,
                              status="active", odds="1:3.2",
                              prize_tiers=[{"value": "$100", "remaining": 9},
                                           {"value": f"${price}", "remaining": 9000}],
                              tier_originals={"100.0": 10, f"{float(price)}": 10000})),
                per_day)
    g.peer_typical_sales = TYPICAL
    return g


def _catalog():
    """A $5 game going back (9001), the best $5 seller PA has stopped shipping
    (9003), and a good one it still ships (9002)."""
    games = [_poor("9001", 5), _fresh("9002", 5, 80_000), _fresh("9003", 5, 95_000),
             _fresh("9010", 10, 90_000)]
    return Catalog(games={g.game_number: g for g in games},
                   captured_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def test_swap_targets_only_offer_orderable_games():
    cat, th, w = _catalog(), Thresholds(), RatingWeights()
    every = [r["game_number"] for r in pa_data.swap_targets(cat, {"9001"}, 5.0, th, w)]
    assert every == ["9003", "9002"]
    same = pa_data.swap_targets(cat, {"9001"}, 5.0, th, w, orderable=None)
    assert [r["game_number"] for r in same] == every          # None filters nothing
    ok = pa_data.swap_targets(cat, {"9001"}, 5.0, th, w, orderable={"9002", "9010"})
    assert [r["game_number"] for r in ok] == ["9002"]


def test_store_rows_swaps_and_flags_follow_the_list():
    cat, th, w = _catalog(), Thresholds(), RatingWeights()
    before = {r["game_number"]: r for r in pa_data.store_rows(cat, {"9001"}, th, w)}
    assert [s["game_number"] for s in before["9001"]["swap_to"]] == ["9003"]
    assert before["9001"]["no_longer_shipped"] is False

    after = {r["game_number"]: r for r in
             pa_data.store_rows(cat, {"9001", "9002"}, th, w, orderable={"9002"})}
    assert after["9001"]["no_longer_shipped"] is True
    assert after["9002"]["no_longer_shipped"] is False
    assert after["9001"]["swap_to"] == []        # 9003 can't be ordered, 9002 carried


def test_bring_in_and_catalog_follow_the_list():
    cat, th, w = _catalog(), Thresholds(), RatingWeights()
    every = pa_data.bring_in_candidates(cat, {"9001"}, th, weights=w)
    assert [r["game_number"] for r in every[5.0]] == ["9003", "9002"]
    assert pa_data.bring_in_candidates(cat, {"9001"}, th, weights=w, orderable=None) == every
    ok = pa_data.bring_in_candidates(cat, {"9001"}, th, weights=w, orderable={"9002"})
    assert [r["game_number"] for r in ok[5.0]] == ["9002"] and 10.0 not in ok

    ranked = {r["game_number"]: r["orderable"] for r in
              pa_data.catalog_rankings(cat, th, w, orderable={"9002"})}
    assert ranked == {"9001": False, "9002": True, "9003": False, "9010": False}
    assert {r["orderable"] for r in pa_data.catalog_rankings(cat, th, w)} == {None}


@pytest.fixture()
def fake_catalog(monkeypatch):
    monkeypatch.setattr(pa_data, "load_catalog", lambda _path: _catalog())


def test_dashboard_suggests_only_orderable_games(app, client, fake_catalog):
    client.post("/inventory/box/1", data={"game_number": "9001"})
    before = client.get("/dashboard").data.decode()
    assert "Fresh 9003" in before                       # no list yet: as before

    _save_list(app, "2026-10-26", [("9002", 5.0, 60), ("9010", 10.0, 60)])
    html = client.get("/dashboard").data.decode()
    assert "Fresh 9003" not in html                     # not orderable: never offered
    swap = html[html.index("Put in instead"):]
    assert "Fresh 9002" in swap[:600]
    board = html[html.index("Best games to bring in"):]
    assert "Fresh 9002" in board and "Fresh 9010" in board


def test_box_page_replacements_are_orderable(app, client, fake_catalog):
    client.post("/inventory/box/1", data={"game_number": "9001"})
    before = client.get("/inventory/box/1").data.decode()
    assert "Fresh 9003" in before and "Put this in instead" in before

    _save_list(app, "2026-10-26", [("9002", 5.0, 60)])
    html = client.get("/inventory/box/1").data.decode()
    assert "Fresh 9003" not in html
    assert "Fresh 9002" in html[html.index("Put this in instead"):]


def test_box_page_steps_to_a_price_with_something_to_order(app, client, fake_catalog):
    client.post("/inventory/box/1", data={"game_number": "9001"})
    _save_list(app, "2026-10-26", [("9010", 10.0, 60)])     # nothing at $5 to order
    html = client.get("/inventory/box/1").data.decode()
    assert "Fresh 9010" in html and "nothing worth carrying at $5" in html


def test_catalog_shows_which_games_can_be_ordered(app, client, fake_catalog):
    before = client.get("/catalog").data.decode()
    assert "can order" not in before and "can&#39;t order" not in before

    client.post("/inventory/add", data={"game_number": "9001"})
    _save_list(app, "2026-10-26", [("9002", 5.0, 60)])
    html = client.get("/catalog").data.decode()
    assert "order list as of Oct 26" in html

    def pill(name):
        at = html.index(name)
        return html[at:at + 300]
    assert "can order" in pill("Fresh 9002")
    assert "can&#39;t order" in pill("Fresh 9003")
    assert "PA no longer ships this" in pill("Slow 9001")      # carried, can't reorder


# --- 3. flag carried games PA no longer ships ---------------------------------

FLAG = "PA no longer ships this"
SENTENCE = "PA's order list as of Oct 26 doesn't include it, so its boxes will run dry."


def _plain(html):
    return " ".join(html.replace("&#39;", "'").split())


def test_no_list_no_flags(client):
    client.post("/inventory/box/1", data={"game_number": "1744"})
    for path in ("/dashboard", "/inventory", "/inventory/box/1", "/catalog/1744"):
        html = _plain(client.get(path).data.decode())
        assert "no longer ships" not in html.lower(), path
        assert "order list" not in html, path


def test_a_carried_game_pa_dropped_is_flagged_everywhere(app, client):
    # 1744 (Goat Load of Cash Crossword) is on sale but not on PA's 10/26 list;
    # 1750 (Cash Spectacular) is on it.
    client.post("/inventory/box/1", data={"game_number": "1744"})
    client.post("/inventory/box/2", data={"game_number": "1750"})
    _save_list(app, "2026-10-26", _pa_list())

    dash = _plain(client.get("/dashboard").data.decode())
    assert FLAG.upper() in dash and SENTENCE in dash
    assert dash.count(FLAG.upper()) == 1                  # 1750 isn't flagged

    inv = _plain(client.get("/inventory").data.decode())
    assert inv.count(FLAG) == 1
    box1 = inv[inv.index("BOX 1"):inv.index("BOX 2")]
    assert FLAG in box1
    assert "as of Oct 26 doesn't include them, so their boxes will run dry" in inv

    box = _plain(client.get("/inventory/box/1").data.decode())
    assert FLAG + "." in box and SENTENCE in box
    assert FLAG not in _plain(client.get("/inventory/box/2").data.decode())

    game = _plain(client.get("/catalog/1744").data.decode())
    assert FLAG + "." in game and SENTENCE in game
    assert FLAG not in _plain(client.get("/catalog/1750").data.decode())

    # The backstock page tells the clerk what to order: never a game PA won't ship.
    back = _plain(client.get("/backstock").data.decode())
    running_out = back[back.index("Running out"):back.index("Held by game")]
    order_now = running_out[running_out.index("Order before the box runs dry"):]
    order_now = order_now[:order_now.index("</ul>")]
    assert "1750" in order_now and "1744" not in order_now
    gone = running_out[running_out.index("</ul>"):]
    assert "1744" in gone and FLAG in gone and "1750" not in gone
    assert ("PA's order list as of Oct 26 doesn't include it, so its boxes will run dry; "
            "it can't be reordered.") in gone
    held = back[back.index("Held by game"):]
    assert held.count(FLAG) == 1                          # 1744's row; 1750 isn't flagged


def test_backstock_without_an_order_list_says_order_as_before(app, client):
    client.post("/inventory/box/1", data={"game_number": "1744"})
    back = _plain(client.get("/backstock").data.decode())
    order_now = back[back.index("Order before the box runs dry"):]
    assert "1744" in order_now[:order_now.index("</ul>")]
    assert "no longer ships" not in back.lower() and "order list" not in back


def test_backstock_only_flags_games_pa_dropped_when_only_those_run_out(app, client):
    client.post("/inventory/box/1", data={"game_number": "1744"})
    _save_list(app, "2026-10-26", _pa_list())
    back = _plain(client.get("/backstock").data.decode())
    assert "Running out" in back and FLAG in back
    assert "Order before the box runs dry" not in back


def test_a_carried_game_without_a_box_is_flagged_too(app, client):
    client.post("/inventory/add", data={"game_number": "1744"})
    _save_list(app, "2026-10-26", _pa_list())
    inv = _plain(client.get("/inventory").data.decode())
    unplaced = inv[inv.index("Carried but not in a box"):]
    assert FLAG in unplaced


def test_a_game_you_dont_carry_just_says_it_cant_be_ordered(app, client):
    _save_list(app, "2026-10-26", _pa_list())
    html = _plain(client.get("/catalog/1744").data.decode())
    assert "Not on PA's order list." in html
    assert "doesn't include it, so it can't be ordered." in html
    assert FLAG not in html
