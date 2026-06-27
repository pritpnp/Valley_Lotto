import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lottery_tracker.model import Game  # noqa: E402


def super7s():
    # Real Super 7s: top-six current remaining (Remaining page) + true originals
    # (PA Bulletin).
    tiers = [("$17,000", 6), ("$1,000", 17), ("$200", 342),
             ("$70", 5962), ("$35", 2837), ("$17", 97307)]
    originals = {"17000.0": 7, "1000.0": 20, "200.0": 360,
                 "70.0": 6360, "35.0": 3000, "17.0": 103200}
    return Game(
        game_number="1789", name="Super 7s",
        prize_tiers=[{"value": v, "remaining": r} for v, r in tiers],
        tier_originals=originals,
    )


def test_tier_health_true_pct():
    g = super7s()
    rows = {r["value"]: r for r in g.tier_health()}
    assert abs(rows["$17,000"]["pct"] - 6 / 7) < 1e-9
    assert abs(rows["$17"]["pct"] - 97307 / 103200) < 1e-9


def test_sell_through_uses_lowest_tier():
    g = super7s()
    # The $17 tier (largest count) anchors sell-through.
    assert abs(g.sell_through_pct - 97307 / 103200) < 1e-9


def test_jackpot_density_top_over_anchor():
    g = super7s()
    expected = (6 / 7) / (97307 / 103200)   # ~0.909 -> top prizes slightly picked over
    assert abs(g.jackpot_density - expected) < 1e-6
    assert g.jackpot_density < 1


def test_weighted_low_health_in_range():
    g = super7s()
    # All tiers sit in the 85-95% band, so the weighted score lands there too.
    assert 0.85 < g.weighted_low_health < 0.96


def test_metrics_none_without_originals():
    g = Game(game_number="x", prize_tiers=[{"value": "$5", "remaining": 3}])
    assert g.jackpot_density is None and g.sell_through_pct is None
    assert g.weighted_low_health is None
