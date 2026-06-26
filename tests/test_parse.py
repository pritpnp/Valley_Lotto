import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lottery_tracker import parse  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def test_parse_remaining_multivalue_columns():
    games = parse.parse_remaining((FIX / "remaining.html").read_text())
    by = {g.game_number: g for g in games}
    assert set(by) == {"5432", "5310", "5500"}

    g = by["5432"]
    assert g.name == "Big Money Bonanza"        # "NEW " prefix stripped
    assert g.price == 20
    assert g.top_prize_value == "$1,000,000"    # first of the six tiers
    assert g.top_prizes_remaining == 5          # first of the wins-remaining list
    assert len(g.prize_tiers) == 6
    assert g.prize_tiers[1] == {"value": "$10,000", "remaining": 17}


def test_parse_remaining_strips_ilottery_suffix():
    games = {g.game_number: g for g in parse.parse_remaining((FIX / "remaining.html").read_text())}
    assert games["5310"].name == "Lucky 7s"     # " Available on iLottery" stripped


def test_parse_sales_ended():
    games = {g.game_number: g for g in parse.parse_sales_ended((FIX / "sales_ended.html").read_text())}
    assert set(games) == {"5210", "5432"}
    assert games["5432"].status == "ended"
    assert games["5432"].sales_end_date == "06/15/2026"


def test_parse_active_strips_new_prefix():
    games = {g.game_number: g for g in parse.parse_active((FIX / "active.html").read_text())}
    assert set(games) == {"5432", "5310", "5500"}
    assert games["5432"].name == "Big Money Bonanza"
    assert all(g.status == "active" for g in games.values())


def test_remaining_missing_columns_raises():
    html = "<table><tr><th>Foo</th><th>Bar</th></tr><tr><td>1</td><td>2</td></tr></table>"
    try:
        parse.parse_remaining(html)
    except parse.ParseError as e:
        assert "Headers seen" in str(e)
    else:
        raise AssertionError("expected ParseError")
