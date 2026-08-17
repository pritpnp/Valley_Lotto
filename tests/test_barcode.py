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


# --- real-world gun output -------------------------------------------------
# The scan gun emits MORE digits than the ticket prints. Captured from a real
# PA ticket scanned on the store's gun: 16 digits = the printed 14 + 2 trailing.
REAL_GUN = "1742011331200893"


def test_parse_real_16_digit_gun_output():
    tc = parse_ticket(REAL_GUN)
    assert tc.game_number == "1742"      # $3M MEGA MOOLAH MULTIPLIER
    assert tc.pack == "0113312"
    assert tc.ticket == 8
    assert tc.extra == "93"              # kept, not discarded
    assert tc.raw == REAL_GUN


def test_printed_14_digit_form_still_parses_with_no_extra():
    tc = parse_ticket("1750-0091798-010")
    assert (tc.game_number, tc.pack, tc.ticket) == ("1750", "0091798", 10)
    assert tc.extra == ""


def test_dashed_form_with_trailing_check_digits():
    tc = parse_ticket("1742-0113312-008-93")
    assert (tc.game_number, tc.pack, tc.ticket, tc.extra) == ("1742", "0113312", 8, "93")


def test_absurd_lengths_rejected():
    for bad in ["12345", "1" * 40, ""]:      # far too short / far too long
        with pytest.raises(BarcodeError):
            parse_ticket(bad)


def test_catalog_flags_a_game_that_does_not_exist():
    """A truncated/misread scan can still split cleanly. When we know the real
    game list, say so rather than silently accepting a bogus game."""
    catalog = {"1742", "1750"}
    tc = parse_ticket("1742011331200893", known_games=catalog)
    assert tc.game_known is True and tc.game_number == "1742"

    bogus = parse_ticket("9999011331200893", known_games=catalog)
    assert bogus.game_known is False            # no split yields a real game -> flagged
    assert parse_ticket("9999011331200893").game_known is None   # unchecked without a catalog


def test_catalog_picks_the_split_that_yields_a_real_game():
    """The decisive check: prefer the segmentation whose game actually exists."""
    # 15 digits: a 5-digit-game reading and a 4-digit-game reading both fit.
    raw = "175000917980107"
    assert parse_ticket(raw, known_games={"1750"}).game_number == "1750"
    assert parse_ticket(raw, known_games={"17500"}).game_number == "17500"


def test_impossible_ticket_number_rejects_that_split():
    """A 'ticket' of 893 can't exist (no pack holds that many), so the parser
    must not choose that segmentation."""
    tc = parse_ticket("1742011331200893")
    assert tc.ticket == 8 and tc.ticket <= 400
