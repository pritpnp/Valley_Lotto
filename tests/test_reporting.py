"""Tests for daily count-session reporting across boxes A1..B24."""

from lottery_tracker.scans import Scan, ScanLog
from lottery_tracker.reporting import daily_report, render_daily_report_md


def _scan(game, pack, ticket, slot, session, t):
    return Scan.from_raw(f"{game}-{pack}-{ticket:03d}", scanned_at=f"2026-08-08T{t}",
                         slot=slot, session=session)


PRICES = {"1750": 30.0, "1744": 5.0}


def _sample_log():
    return ScanLog(scans=[
        # Box A1: game 1750 ($30), same pack all day
        _scan("1750", "0091798", 10, "A1", "morning", "08:00Z"),
        _scan("1750", "0091798", 18, "A1", "midday",  "13:00Z"),
        _scan("1750", "0091798", 25, "A1", "night",   "21:00Z"),
        # Box A2: game 1744 ($5), same pack all day
        _scan("1744", "0100200", 5,  "A2", "morning", "08:00Z"),
        _scan("1744", "0100200", 20, "A2", "midday",  "13:00Z"),
        _scan("1744", "0100200", 40, "A2", "night",   "21:00Z"),
    ])


def test_daily_totals_and_per_box():
    rep = daily_report(_sample_log(), "2026-08-08", prices=PRICES)
    assert rep.total_tickets == 15 + 35            # A1 sold 15, A2 sold 35
    assert rep.total_revenue == 450.0 + 175.0

    by_slot = {r.slot: r for r in rep.rows}
    assert by_slot["A1"].total_sold == 15 and by_slot["A1"].revenue == 450.0
    assert by_slot["A2"].total_sold == 35 and by_slot["A2"].revenue == 175.0


def test_per_interval_breakdown():
    rep = daily_report(_sample_log(), "2026-08-08", prices=PRICES)
    a1 = {r.slot: r for r in rep.rows}["A1"]
    # morning->midday = 8, midday->night = 7
    assert [iv.sold for iv in a1.intervals] == [8, 7]
    assert [(iv.frm, iv.to) for iv in a1.intervals] == [("morning", "midday"), ("midday", "night")]


def test_per_game_rollup_and_box_order():
    rep = daily_report(_sample_log(), "2026-08-08", prices=PRICES)
    assert rep.per_game["1750"]["tickets"] == 15
    assert rep.per_game["1744"]["revenue"] == 175.0
    assert [r.slot for r in rep.rows] == ["A1", "A2"]   # natural box order


def test_rollover_within_a_box_is_bridged_and_flagged():
    log = ScanLog(scans=[
        _scan("1744", "0100200", 55, "B7", "morning", "08:00Z"),  # near end ($5 pack ~60)
        _scan("1744", "0100200", 59, "B7", "midday",  "13:00Z"),  # learns size 60
        _scan("1744", "0100201", 8,  "B7", "night",   "21:00Z"),  # new pack
    ])
    rep = daily_report(log, "2026-08-08", prices=PRICES)
    b7 = rep.rows[0]
    assert b7.estimated is True
    # 55->59 = 4, rollover (60-59=1) + 8 = 9  => 13
    assert b7.total_sold == 13
    assert b7.pack_size_used == 60


def test_render_markdown_smoke():
    md = render_daily_report_md(daily_report(_sample_log(), "2026-08-08", prices=PRICES))
    assert "Daily scratch-off sales" in md
    assert "A1" in md and "A2" in md
    assert "morning:010" in md   # count positions shown
