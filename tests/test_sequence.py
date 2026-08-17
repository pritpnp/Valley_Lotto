"""Tests for log-based pack learning and sold-over-a-sequence.

These cover the "just keep a log and detect the reset" approach: pack changes are
known from the pack-ID field, and pack size is learned from the peak ticket seen.
"""

from lottery_tracker.barcode import parse_ticket
from lottery_tracker.scans import (
    Scan, ScanLog, learn_pack_size, learn_pack_sizes, sold_over_sequence,
)


def _s(pack, ticket, t):
    return Scan.from_ticket(parse_ticket(f"1750-{pack}-{ticket:03d}"),
                            scanned_at=f"2026-08-07T{t}", kind="single")


def test_learn_pack_size_is_peak_plus_one():
    scans = [_s("0091798", 10, "08:00Z"), _s("0091798", 59, "12:00Z")]
    assert learn_pack_size(scans) == 60          # peak 59 -> size 60
    assert learn_pack_size([]) is None


def test_learn_pack_sizes_per_game():
    log = ScanLog(scans=[_s("0091798", 59, "08:00Z"), _s("0091799", 3, "09:00Z")])
    assert learn_pack_sizes(log) == {"1750": 60}


def test_simple_same_pack_run():
    scans = [_s("0091798", 10, "08:00Z"), _s("0091798", 25, "20:00Z")]
    res = sold_over_sequence(scans, price=30.0)
    assert res.tickets_sold == 15
    assert res.revenue == 450.0
    assert res.pack_changes == 0
    assert res.estimated is False


def test_rollover_detected_by_pack_id_and_bridged_with_learned_size():
    # Peak 59 seen before the reset teaches size=60; then a new pack starts at 3.
    scans = [
        _s("0091798", 50, "08:00Z"),
        _s("0091798", 59, "14:00Z"),   # near end of old pack -> learns size 60
        _s("0091799", 3,  "20:00Z"),   # new pack (id changed, ticket reset)
    ]
    res = sold_over_sequence(scans, price=30.0)
    # 50->59 = 9, rollover: old tail (60-59=1) + new 3 = 4  => 13 total
    assert res.pack_changes == 1
    assert res.pack_size_used == 60
    assert res.tickets_sold == 13
    assert res.revenue == 390.0
    assert res.estimated is True


def test_cold_start_rollover_without_known_size_counts_new_pack_only():
    # First-ever data: open a pack low, then a new pack appears. We never saw the
    # old pack near its end, so its tail can't be counted yet — noted, not guessed.
    scans = [_s("0091798", 5, "08:00Z"), _s("0091799", 2, "20:00Z")]
    res = sold_over_sequence(scans)  # learned size = max(5,2)+1 = 6, but 6 !> a.ticket(5)? 6>5 so tail=1
    # size learned = 6, old tail = 6-5 = 1, new = 2 -> 3. estimated.
    assert res.pack_changes == 1
    assert res.estimated is True


def test_explicit_pack_size_overrides_learned():
    scans = [_s("0091798", 50, "08:00Z"), _s("0091799", 4, "20:00Z")]
    res = sold_over_sequence(scans, price=30.0, pack_size=60)
    # old tail 60-50=10 + new 4 = 14
    assert res.pack_size_used == 60
    assert res.tickets_sold == 14
