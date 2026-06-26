import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lottery_tracker import parse  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def test_parse_remaining_maps_columns_by_header():
    games = parse.parse_remaining((FIX / "remaining.html").read_text())
    by = {g.game_number: g for g in games}
    assert set(by) == {"5432", "5310", "5500"}

    g = by["5432"]
    assert g.name == "Big Money Bonanza"
    assert g.price == 20
    assert g.top_prize_value == "$1,000,000"
    assert g.top_prizes_total == 8
    assert g.top_prizes_remaining == 5
    assert g.total_prizes_remaining == 900000
    assert g.total_prizes_original == 1200000
    assert abs(g.top_prize_pct_remaining - 5 / 8) < 1e-9


def test_parse_sales_ended():
    games = parse.parse_sales_ended((FIX / "sales_ended.html").read_text())
    by = {g.game_number: g for g in games}
    assert set(by) == {"5210", "5432"}
    assert by["5432"].status == "ended"
    assert by["5432"].claim_deadline == "08/14/2026"
    assert by["5210"].sales_end_date == "05/01/2026"


def test_parse_active():
    games = parse.parse_active((FIX / "active.html").read_text())
    assert {g.game_number for g in games} == {"5432", "5310", "5500"}
    assert all(g.status == "active" for g in games)


def test_unrecognized_table_raises_with_headers():
    html = "<table><tr><th>Foo</th><th>Bar</th></tr><tr><td>1</td><td>2</td></tr></table>"
    try:
        parse.parse_remaining(html)
    except parse.ParseError as e:
        assert "Headers seen" in str(e)
    else:
        raise AssertionError("expected ParseError")
