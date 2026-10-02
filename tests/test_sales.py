"""Sales a day, from PA's weekly prize counts (sales.py)."""

from datetime import datetime, timedelta, timezone

from lottery_tracker import sales
from lottery_tracker.model import Game
from _games import verified

T0 = datetime(2026, 9, 1, 19, 0, tzinfo=timezone.utc)


def _game(**kw):
    # Printed: 1,000 of $100 and 9,000 of $20 -> 10,000 published prizes; odds 1 in
    # 4 over 40,000 tickets... the helper sets tickets_printed to agree with the odds.
    g = Game(game_number="1", name="Test", price=5, status="active",
             prize_tiers=[{"value": "$100", "remaining": 0}, {"value": "$20", "remaining": 0}],
             tier_originals={"100.0": 1000, "20.0": 9000}, **kw)
    return verified(g, odds=4.0)


def _snap(day: float, left100: int, left20: int, num="1", **extra):
    at = (T0 + timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%SZ")
    games = {num: {"prize_tiers": [{"value": "$100", "remaining": left100},
                                   {"value": "$20", "remaining": left20}]}}
    games.update(extra)
    return {"captured_at": at, "games": games}


def test_a_steady_game_is_measured_exactly():
    g = _game()
    assert g.tickets_printed == 40_000
    # 100 published prizes claimed a day = 1% of the 10,000 printed a day
    # = 400 tickets a day = $2,000 a day at $5.
    snaps = [_snap(7 * w, 900 - 10 * 7 * w, 8100 - 90 * 7 * w) for w in range(5)]
    p = sales.measure(g, sales.readings_from(snaps))
    assert abs(p.tickets_per_day - 400) < 1e-6
    assert abs(p.dollars_per_day - 2000) < 1e-6
    assert p.uncertainty == 0 and p.readings == 5 and p.days == 28


def test_repeated_scrapes_of_one_posting_count_once():
    a = _snap(0, 900, 8100)
    again = dict(a, captured_at=(T0 + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    later = _snap(7, 830, 7470)
    r = sales.readings_from([later, again, a])          # any order in
    assert [x.at for x in r] == [T0, T0 + timedelta(days=7)]


def test_the_give_or_take_is_the_lines_standard_error():
    g = _game()
    left = [9000, 8290, 7610, 6880]                      # roughly 100 a day, not exactly
    snaps = [_snap(7 * w, 0, v) for w, v in enumerate(left)]
    p = sales.measure(g, sales.readings_from(snaps))
    xs, ys = [0, 7, 14, 21], [-v for v in left]
    n, mx, my = 4, sum(xs) / 4, sum(ys) / 4
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    rss = sum((y - my - b * (x - mx)) ** 2 for x, y in zip(xs, ys))
    se = (rss / (n - 2) / sxx) ** 0.5
    assert abs(p.uncertainty - se / b) < 1e-9
    assert abs(p.dollars_per_day - b / 10_000 * 40_000 * 5) < 1e-6


def test_too_few_postings_are_not_measured_and_say_why():
    g = _game()
    p = sales.measure(g, sales.readings_from([_snap(0, 900, 8100), _snap(7, 800, 7000)]))
    assert p.dollars_per_day is None and "it takes 3" in p.why
    # Three postings, but only three days apart.
    p = sales.measure(g, sales.readings_from([_snap(d, 900 - d, 8100 - d) for d in (0, 1, 3)]))
    assert p.dollars_per_day is None and "7 days apart" in p.why


def test_only_the_last_five_weeks_count():
    g = _game()
    old = [_snap(d, 500, 500) for d in (0, 7)]           # long ago, very different pace
    recent = [_snap(60 + 7 * w, 900 - 70 * w, 8100 - 630 * w) for w in range(5)]
    p = sales.measure(g, sales.readings_from(old + recent))
    assert p.readings == 5 and abs(p.dollars_per_day - 2000) < 1e-6


def test_counts_that_rise_are_not_read_as_sales():
    g = _game()
    snaps = [_snap(7 * w, 900, 8100 + 50 * w) for w in range(4)]
    p = sales.measure(g, sales.readings_from(snaps))
    assert p.dollars_per_day is None and "went up" in p.why


def test_nothing_claimed_is_zero_sales_not_unknown():
    g = _game()
    snaps = [_snap(7 * w, 900, 8100, other={"prize_tiers": [{"value": "$5", "remaining": 99 - w}]})
             for w in range(4)]
    p = sales.measure(g, sales.readings_from(snaps))
    assert p.dollars_per_day == 0.0


def test_an_unchecked_prize_table_is_never_used():
    g = _game()
    g.odds = "1:9.0"                          # disagrees with tickets ÷ prizes printed
    assert not g.printed_table_trusted
    snaps = [_snap(7 * w, 900 - 70 * w, 8100 - 630 * w) for w in range(5)]
    p = sales.measure(g, sales.readings_from(snaps))
    assert p.dollars_per_day is None and "couldn't be checked" in p.why


def test_a_posting_missing_a_prize_level_is_skipped():
    g = _game()
    snaps = [_snap(7 * w, 900 - 70 * w, 8100 - 630 * w) for w in range(5)]
    snaps[2]["games"]["1"]["prize_tiers"] = [{"value": "$20", "remaining": 1}]   # half a reading
    p = sales.measure(g, sales.readings_from(snaps))
    assert p.readings == 4 and abs(p.dollars_per_day - 2000) < 1e-6


def test_attach_records_it_on_games_on_sale_only():
    on = _game()
    off = _game()
    off.game_number, off.status = "2", "ended"
    snaps = [_snap(7 * w, 900 - 70 * w, 8100 - 630 * w) for w in range(5)]
    sales.attach({"1": on, "2": off}, sales.readings_from(snaps))
    assert on.sales_per_day == 2000 and on.sales_readings == 5 and on.sales_why is None
    assert off.sales_per_day is None
    # Saved with the game, and read back.
    back = Game.from_dict(on.to_dict())
    assert back.sales_per_day == 2000 and back.sales_days == 28


def test_load_readings_skips_unreadable_files(tmp_path):
    import json
    (tmp_path / "a.json").write_text(json.dumps(_snap(0, 900, 8100)))
    (tmp_path / "b.json").write_text("{not json")
    r = sales.load_readings(tmp_path, _snap(7, 830, 7470))
    assert len(r) == 2
