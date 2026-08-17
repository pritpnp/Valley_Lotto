"""Tests for barcode parsing and open/close sold-math.

The canonical fixture is a real ticket scanned on this project:
``1750-0091798-010`` (game 1750 CASH SPECTACULAR, pack 0091798, ticket 10).
"""

import pytest

from lottery_tracker.barcode import parse_ticket, try_parse_ticket, BarcodeError
from lottery_tracker.scans import Scan, compute_sold, ScanLog, load_scans, save_scans


REAL = "1750-0091798-010"


def test_parse_real_ticket():
    tc = parse_ticket(REAL)
    assert tc.game_number == "1750"
    assert tc.pack == "0091798"
    assert tc.ticket == 10
    assert tc.raw == REAL
    assert tc.game_padded == "1750"


def test_parse_tolerates_gun_noise():
    # trailing newline / spaces / wrapping quotes a scanner might add
    for variant in ["1750-0091798-010\n", "  1750-0091798-010 ", "'1750-0091798-010'",
                    "1750 0091798 010"]:
        tc = parse_ticket(variant)
        assert (tc.game_number, tc.pack, tc.ticket) == ("1750", "0091798", 10)


def test_parse_digits_only_form():
    # a gun that strips the dashes -> bare 14-digit run, split by fixed widths
    tc = parse_ticket("17500091798010")
    assert (tc.game_number, tc.pack, tc.ticket) == ("1750", "0091798", 10)


def test_game_number_normalized():
    tc = parse_ticket("0993-0000001-000")
    assert tc.game_number == "993"      # matches un-padded config keys
    assert tc.game_padded == "0993"     # still recoverable in barcode form


def test_bad_input_raises_and_try_returns_none():
    for bad in ["", "   ", "hello", "1750-0091798", "12345678901234567890"]:
        with pytest.raises(BarcodeError):
            parse_ticket(bad)
        assert try_parse_ticket(bad) is None


def _scan(pack, ticket, kind):
    return Scan.from_ticket(
        parse_ticket(f"1750-{pack}-{ticket:03d}"),
        scanned_at="2026-08-07T08:00:00Z", kind=kind,
    )


def test_sold_same_pack():
    open_s = _scan("0091798", 10, "open")
    close_s = _scan("0091798", 25, "close")
    res = compute_sold(open_s, close_s, price=30.0)
    assert res.same_pack is True
    assert res.tickets_sold == 15          # abs delta, direction unknown
    assert res.revenue == 450.0            # 15 * $30
    assert res.direction == "up"


def test_sold_pack_change_is_flagged_not_guessed():
    open_s = _scan("0091798", 10, "open")
    close_s = _scan("0091799", 3, "close")   # new pack loaded mid-period
    res = compute_sold(open_s, close_s, price=30.0)
    assert res.same_pack is False
    assert res.tickets_sold is None
    assert res.revenue is None
    assert "pack changed" in res.note


def test_scanlog_roundtrip(tmp_path):
    log = ScanLog()
    log.add(_scan("0091798", 10, "open"))
    log.add(_scan("0091798", 25, "close"))
    path = tmp_path / "scans.json"
    save_scans(path, log)

    reloaded = load_scans(path)
    assert len(reloaded.scans) == 2
    assert reloaded.for_pack("0091798")[0].ticket == 10
    # tolerate unknown keys from a future schema
    assert Scan.from_dict({"game_number": "1750", "pack": "x", "ticket": 1,
                           "scanned_at": "t", "future_field": 99}).ticket == 1
