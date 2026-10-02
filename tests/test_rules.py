import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lottery_tracker.model import Game, merge_games  # noqa: E402
from _games import selling, verified  # noqa: E402
from lottery_tracker.rules import Thresholds, evaluate, recommendation  # noqa: E402


def _g(num, **kw):
    return Game(game_number=num, **kw)


def test_recommendation_send_back_when_ended():
    g = _g("1", status="ended", sales_end_date="06/15/2026")
    act, _ = recommendation(g, Thresholds())
    assert act == "send_back"


def test_recommendation_send_back_when_it_sells_under_the_line():
    # Three $5 games; the slow one sells 10% of the typical (median) one.
    from lottery_tracker.model import compare_with_peers
    games = compare_with_peers({n: selling(_g(n, price=5, status="active"), d)
                                for n, d in (("1", 3_000), ("2", 30_000), ("3", 60_000))})
    act, reason = recommendation(games["1"], Thresholds())
    assert act == "send_back"
    # the reason is written for whoever reads it, not in shorthand
    assert "$3,000 a day" in reason and "10% of the typical $5 game" in reason
    assert recommendation(games["2"], Thresholds())[0] == "keep"
    # Exactly at the line is kept; just under it is not.
    games["1"].sales_per_day = 6_000
    assert recommendation(compare_with_peers(games)["1"], Thresholds())[0] == "keep"


def test_recommendation_keep_when_healthy():
    g = _g("1", status="active", top_prizes_total=10, top_prizes_remaining=8, odds="1:3.2")
    assert recommendation(g, Thresholds())[0] == "keep"


def test_new_game_alert_when_it_appears():
    prev = {"1": _g("1", status="active")}
    cur = {"1": _g("1", status="active"),
           "2": _g("2", name="Fresh Game", status="active", price=5, odds="1:3.3")}
    alerts = evaluate(cur, prev, inventory=set(), thresholds=Thresholds())
    new = [a for a in alerts if a.kind == "new"]
    assert len(new) == 1 and new[0].game_number == "2"


def mk(num, **kw):
    return Game(game_number=num, **kw)


def test_owned_game_ending_is_critical():
    prev = {"5432": mk("5432", name="Big Money", status="active")}
    cur = {"5432": mk("5432", name="Big Money", status="ended", claim_deadline="08/14/2026")}
    alerts = evaluate(cur, prev, inventory={"5432"}, thresholds=Thresholds())
    assert len(alerts) == 1
    a = alerts[0]
    assert a.kind == "ended" and a.owned and a.severity.value == "critical"


def test_ended_only_alerts_once():
    prev = {"5432": mk("5432", status="ended")}
    cur = {"5432": mk("5432", status="ended")}
    assert evaluate(cur, prev, inventory={"5432"}, thresholds=Thresholds()) == []


def test_low_prize_transition_by_pct():
    th = Thresholds(top_prize_pct=0.25, top_prize_count_floor=0)
    def at(left):
        return verified(mk("5310", status="active", top_prizes_remaining=left,
                           prize_tiers=[{"value": "$1,000", "remaining": left},
                                        {"value": "$5", "remaining": 5000}],
                           tier_originals={"1000.0": 10, "5.0": 10000}))
    alerts = evaluate({"5310": at(1)}, {"5310": at(3)}, inventory={"5310"}, thresholds=th)
    assert len(alerts) == 1 and alerts[0].kind == "low_prizes" and alerts[0].owned
    assert "(1/10)" in alerts[0].message      # the count printed for the SAME prize


def test_low_prize_by_count_floor():
    th = Thresholds(top_prize_pct=None, top_prize_count_floor=1)
    prev = {"5310": mk("5310", status="active", top_prizes_remaining=3)}
    cur = {"5310": mk("5310", status="active", top_prizes_remaining=1)}
    assert len(evaluate(cur, prev, inventory={"5310"}, thresholds=th)) == 1


def test_low_prize_not_repeated():
    th = Thresholds(top_prize_pct=None, top_prize_count_floor=1)
    prev = {"5310": mk("5310", status="active", top_prizes_remaining=1)}
    cur = {"5310": mk("5310", status="active", top_prizes_remaining=1)}
    assert evaluate(cur, prev, inventory={"5310"}, thresholds=th) == []


def test_non_inventory_low_ignored_unless_report_all():
    th = Thresholds(top_prize_pct=None, top_prize_count_floor=1)
    prev = {"5500": mk("5500", status="active", top_prizes_remaining=5)}
    cur = {"5500": mk("5500", status="active", top_prizes_remaining=1)}
    assert evaluate(cur, prev, inventory=set(), thresholds=th) == []
    a = evaluate(cur, prev, inventory=set(), thresholds=th, report_all_games=True)
    assert len(a) == 1 and not a[0].owned


def test_owned_game_vanishing_flags_removed():
    prev = {"5310": mk("5310", name="Lucky 7s", status="active")}
    alerts = evaluate({}, prev, inventory={"5310"}, thresholds=Thresholds())
    assert len(alerts) == 1 and alerts[0].kind == "removed"


def test_activeprint_is_authority_for_status():
    # RULE #1: on the ActivePrint list => active; absent => dead, no matter what
    # the other pages say.
    remaining = [
        mk("5432", name="Big Money", status="active", top_prizes_remaining=5),
        mk("5310", name="Lucky 7s", status="active", top_prizes_remaining=2),
    ]
    active = [mk("5432", name="Big Money", status="active")]   # only 5432 is still selling
    ended = [mk("5310", name="Lucky 7s", status="ended", sales_end_date="06/15/2026")]
    merged = merge_games(remaining, ended, active)
    assert merged["5432"].status == "active"   # on ActivePrint -> alive
    assert merged["5432"].top_prizes_remaining == 5
    assert merged["5310"].status == "ended"    # NOT on ActivePrint -> dead
    assert merged["5310"].sales_end_date == "06/15/2026"


def test_a_top_prize_count_is_never_invented():
    """Without PA's printed count for the same prize, the share of top prizes
    left is unknown. It used to be estimated from the highest count ever seen,
    and paired with counts for other prizes; both were guesses."""
    g = mk("5432", status="active", top_prizes_remaining=2, top_prizes_total=8,
           prize_tiers=[{"value": "$5,000", "remaining": 2}])
    assert g.top_prize_pct_remaining is None


def test_the_top_prize_is_matched_by_dollar_value():
    """A "top prizes" count for a prize PA doesn't list (Keys and Cash's
    $100,000) must never be paired with the one it does ($5,000)."""
    g = verified(mk("1693", status="active",
                    prize_tiers=[{"value": "$5,000", "remaining": 6}, {"value": "$100", "remaining": 1663}],
                    tier_originals={"100000.0": 20, "5000.0": 30, "100.0": 18000}), odds=4.42)
    assert g.top_prize_pair == (6, 30)


def test_a_table_that_contradicts_pa_is_not_used():
    """Ca$h Money's table was misread: 54,829 "left" of 1,000 printed, and odds
    of 1 in 1,500 against PA's 1 in 3.63. Nothing may be built on it."""
    g = mk("1796", status="active", odds="1:3.63", tickets_printed=5_400_000,
           prize_tiers=[{"value": "$500", "remaining": 54829}],
           tier_originals={"500.0": 1000, "100.0": 2600})
    assert not g.printed_table_trusted
    assert g.overall_pct_remaining is None and g.top_prize_pct_remaining is None


# --- the wording a clerk actually reads --------------------------------------

