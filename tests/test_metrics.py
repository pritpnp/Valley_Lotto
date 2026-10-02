import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lottery_tracker.model import Game  # noqa: E402
from _games import selling, verified  # noqa: E402


def super7s():
    # Real Super 7s: top-six current remaining (Remaining page) + true originals
    # (PA Bulletin).
    tiers = [("$17,000", 6), ("$1,000", 17), ("$200", 342),
             ("$70", 5962), ("$35", 2837), ("$17", 97307)]
    originals = {"17000.0": 7, "1000.0": 20, "200.0": 360,
                 "70.0": 6360, "35.0": 3000, "17.0": 103200}
    return verified(Game(
        game_number="1789", name="Super 7s",
        prize_tiers=[{"value": v, "remaining": r} for v, r in tiers],
        tier_originals=originals,
    ), odds=3.38)


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


def mega_moolah():
    # The user's real $3M Mega Moolah Multiplier tier structure.
    tiers = [("$3,000,000", 2), ("$300,000", 6), ("$30,000", 4),
             ("$3,000", 237), ("$1,000", 2306), ("$500", 856)]
    originals = {"3000000.0": 3, "300000.0": 15, "30000.0": 15,
                 "3000.0": 600, "1000.0": 5960, "500.0": 2320}
    return verified(Game(game_number="1742", name="Mega Moolah", price=30, status="active",
                         odds="1:3.49",
                         prize_tiers=[{"value": v, "remaining": r} for v, r in tiers],
                         tier_originals=originals))


def test_overall_pct_is_count_weighted():
    g = mega_moolah()
    rem = 2 + 6 + 4 + 237 + 2306 + 856        # 3411
    orig = 3 + 15 + 15 + 600 + 5960 + 2320    # 8913
    assert abs(g.overall_pct_remaining - rem / orig) < 1e-9
    # ~38%, NOT the 67% the top-prize-only view showed.
    assert 0.37 < g.overall_pct_remaining < 0.39


def test_top_tier_skew_is_noise_not_signal():
    g = mega_moolah()
    zs = {round(r["value_num"]): r for r in g.tier_z_scores()}
    top = zs[3000000]
    # 2-of-3 looks dramatic but is statistically indistinguishable from the game.
    assert abs(top["z"]) < 2 and not top["significant"]
    # And therefore jackpot density (which divides by sell-through) is flagged noise.
    assert not g.jackpot_density_significant


def test_significant_low_prize_outlier_is_caught():
    # A game uniformly ~80% left, EXCEPT the cheap tier is gutted (way more than
    # sampling noise) -> that tier must be flagged significant & negative.
    tiers = [{"value": "$1000", "remaining": 8},      # ~80% of 10
             {"value": "$5", "remaining": 2000}]      # only 20% of 10,000 -> outlier
    g = verified(Game(game_number="z", status="active", odds="1:3.5",
                      prize_tiers=tiers, tier_originals={"1000.0": 10, "5.0": 10000}))
    cheap = [r for r in g.tier_z_scores() if r["value_num"] == 5.0][0]
    assert cheap["significant"] and cheap["z"] < -2


def test_the_rating_is_sales_against_the_typical_game_at_its_price():
    from lottery_tracker.model import compare_with_peers
    from lottery_tracker.rules import RatingWeights, rate
    games = {n: selling(Game(game_number=n, price=30, status="active"), d)
             for n, d in (("a", 10_000), ("b", 40_000), ("c", 90_000))}
    g = selling(mega_moolah(), 2_000)
    games[g.game_number] = g
    compare_with_peers(games)
    assert g.peer_typical_sales == 25_000            # median of 2K, 10K, 40K, 90K
    score, facts = rate(g, RatingWeights())
    assert abs(score - 100 * 2_000 / 25_000) < 1e-9  # 8: sells 8% of typical
    assert facts[0].key == "sales" and facts[0].weight == 100
    # A strong seller stops at 100.
    assert rate(games["c"])[0] == 100


def test_prize_facts_are_shown_but_do_not_change_the_rating():
    from lottery_tracker.model import compare_with_peers
    from lottery_tracker.rules import rate
    a = selling(mega_moolah(), 5_000)
    b = selling(Game(game_number="b", price=30, status="active"), 20_000)
    compare_with_peers({"a": a, "b": b})
    score, facts = rate(a)
    shown = {f.key: f for f in facts}
    assert {"prizes_left", "win_back", "top_prizes", "odds"} <= set(shown)
    assert all(shown[k].weight == 0 for k in ("prizes_left", "win_back", "top_prizes", "odds"))
    # Gutting the prizes changes nothing: only sales decide.
    a.prize_tiers = [{"value": t["value"], "remaining": 0} for t in a.prize_tiers]
    assert rate(a)[0] == score


def test_unmeasured_sales_mean_no_rating_and_say_why():
    from lottery_tracker.model import compare_with_peers
    from lottery_tracker.rules import Thresholds, rate, recommendation
    g = mega_moolah()
    g.sales_why = "PA has posted 1 prize count for this game in the last 35 days."
    compare_with_peers({"x": g})
    score, facts = rate(g)
    assert score is None and facts[0].note == g.sales_why
    action, reason = recommendation(g, Thresholds())
    assert action == "keep" and g.sales_why in reason


def _bulletin_game(num, price, odds, frac):
    # A game where every tier has `frac` of its prizes left.
    tiers = [{"value": "$1000", "remaining": int(10 * frac)},
             {"value": "$20", "remaining": int(100000 * frac)}]
    return verified(Game(game_number=num, price=price, status="active", odds=odds,
                         prize_tiers=tiers, tier_originals={"1000.0": 10, "20.0": 100000}))


def test_bring_in_candidates_ranks_fresh_by_odds():
    from lottery_tracker.notify import bring_in_candidates
    games = {
        "10": _bulletin_game("10", 5, "1:3.2", 0.9),   # fresh, great odds
        "11": _bulletin_game("11", 5, "1:4.8", 0.9),   # fresh, worse odds
        "12": _bulletin_game("12", 5, "1:3.0", 0.2),   # great odds but picked over -> excluded
        "99": _bulletin_game("99", 5, "1:3.1", 0.95),  # but this one is in inventory -> excluded
    }
    out = bring_in_candidates(games, inventory={"99"}, min_left=0.6, per_price=4)
    ranked = [g.game_number for g in out[5]]
    assert ranked == ["10", "11"]   # 12 too depleted, 99 owned
