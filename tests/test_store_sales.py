"""A store's own sales take over from PA-wide sales once there's enough of them."""

from types import SimpleNamespace as NS

from lottery_tracker import store_sales
from lottery_tracker.model import Game, compare_with_peers
from lottery_tracker.rules import Thresholds, rate, recommendation
from _games import selling


def _row(game, revenue, price=5, counts=2):
    return NS(game_number=game, revenue=revenue, price=price, counts=[{}] * counts)


def _reports(days, rows_for_day):
    return [NS(date=f"2026-10-{d + 1:02d}", rows=rows_for_day(d)) for d in range(days)]


def _three_games(d):
    return [_row("slow", 10), _row("mid", 100), _row("fast", 300)]


def test_a_box_day_is_its_sales_and_the_typical_box_is_the_median():
    out = store_sales.measure(_reports(14, _three_games))
    assert out["slow"].per_box_day == 10 and out["slow"].typical == 100
    assert out["slow"].box_days == 14 and out["slow"].days == 14


def test_nothing_until_the_store_has_two_weeks():
    assert store_sales.measure(_reports(13, _three_games)) == {}


def test_a_box_counted_once_that_day_is_not_measured():
    reps = _reports(14, lambda d: _three_games(d) + [_row("once", 999, counts=1)])
    assert "once" not in store_sales.measure(reps)


def test_a_game_needs_a_week_of_box_days():
    reps = _reports(14, lambda d: _three_games(d) + ([_row("new", 50)] if d >= 8 else []))
    assert "new" not in store_sales.measure(reps)          # only 6 box-days
    reps = _reports(14, lambda d: _three_games(d) + ([_row("new", 50)] if d >= 7 else []))
    assert "new" in store_sales.measure(reps)


def test_too_few_games_at_a_price_keeps_pa_wide():
    reps = _reports(14, lambda d: _three_games(d) + [_row("big", 500, price=30)])
    out = store_sales.measure(reps)
    assert "big" not in out and "slow" in out


def test_two_boxes_of_one_game_count_as_two_box_days():
    reps = _reports(14, lambda d: _three_games(d) + [_row("slow", 30)])
    s = store_sales.measure(reps)["slow"]
    assert s.box_days == 28 and s.per_box_day == 20


def _pa(slow_per_day):
    g = selling(Game(game_number="slow", name="Slow", price=5, status="active"), slow_per_day)
    others = {n: selling(Game(game_number=n, price=5, status="active"), 50_000)
              for n in ("x", "y")}
    compare_with_peers({"slow": g, **others})
    return g


def test_with_store_counts_the_rating_is_half_pa_half_store():
    own = store_sales.measure(_reports(14, _three_games))["slow"]   # $10 vs $100: 10
    g = _pa(15_000)                                                  # 30% of $50K: 30
    assert rate(g)[0] == 30                                          # PA-wide alone
    score, facts = rate(g, own=own)
    assert score == 20                                               # (10 + 30) / 2
    voted = {f.key: f.weight for f in facts if f.weight > 0}
    assert voted == {"store_sales": 50, "sales": 50}
    assert "In your store" in facts[0].note


def test_both_have_to_be_low_enough_together_to_send_back():
    own = store_sales.measure(_reports(14, _three_games))["slow"]   # store: 10
    strong = _pa(100_000)                                            # PA: 100
    action, reason = recommendation(strong, Thresholds(), own=own)
    assert action == "keep"                                          # (10 + 100) / 2 = 55
    assert "your typical $5 box" in reason and "across PA" in reason
    assert "Both together: 55 out of 100" in reason
    weak = _pa(5_000)                                                # PA: 10
    action, reason = recommendation(weak, Thresholds(), own=own)
    assert action == "send_back" and "Both together: 10 out of 100" in reason


def test_store_figure_alone_rates_a_game_pa_cannot_measure():
    own = store_sales.measure(_reports(14, _three_games))["slow"]
    g = Game(game_number="slow", price=5, status="active", sales_why="too new")
    compare_with_peers({"slow": g})
    assert rate(g, own=own)[0] == 10


def test_the_app_switches_to_the_stores_own_sales_after_two_weeks(tmp_path):
    """End to end: two weeks of morning and night counts in the app's own
    database, and the box page decides on them."""
    import json
    import pathlib
    from datetime import date, timedelta
    from lottery_tracker.web.app import create_app
    from lottery_tracker.web.models import ScanRow

    games = json.loads(pathlib.Path("data/state.json").read_text())["games"]
    fives = sorted(n for n, g in games.items() if g.get("status") == "active" and g.get("price") == 5)
    if len(fives) < 3:
        import pytest
        pytest.skip("needs three $5 games in the bundled catalog")
    slow, mid, fast = fives[:3]

    app = create_app({"DATABASE_URL": f"sqlite:///{tmp_path/'s.db'}", "SECRET_KEY": "k",
                      "DEFAULT_STORE": "t", "SLOTS": "3", "REGISTER_CODE": None,
                      "TIMEZONE": "UTC"})
    app.config.update(TESTING=True)
    c = app.test_client()
    c.post("/register", data={"username": "prit", "password": "pw"})
    for slot, g in (("1", slow), ("2", mid), ("3", fast)):
        c.post(f"/inventory/box/{slot}", data={"game_number": g})

    sold = {slow: 2, mid: 20, fast: 40}                      # tickets a day
    today = date.today()
    with app.config["SESSION_FACTORY"]() as db:
        for back in range(1, 15):                            # the 14 days before today
            d = (today - timedelta(days=back)).isoformat()
            for slot, g in (("1", slow), ("2", mid), ("3", fast)):
                for session, t, hh in (("morning", 0, "13"), ("night", sold[g], "23")):
                    db.add(ScanRow(store="t", game_number=g, pack=f"99{slot}{back:04d}",
                                   ticket=t, slot=slot, session=session,
                                   scanned_at=f"{d}T{hh}:00:00Z", raw=""))
        db.commit()

    html = " ".join(c.get("/inventory/box/1").data.decode().split())
    assert "In your store this game sells about $10 a box a day" in html
    assert "your typical $5 box ($100)" in html
    if games[slow].get("sales_per_day") is not None:     # PA-wide counts too, equally
        assert "Counts for 50% of the rating." in html
