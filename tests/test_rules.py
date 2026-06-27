import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lottery_tracker.model import Game, estimate_top_prize_totals, merge_games  # noqa: E402
from lottery_tracker.rules import Thresholds, evaluate, recommendation  # noqa: E402


def _g(num, **kw):
    return Game(game_number=num, **kw)


def test_recommendation_send_back_when_ended():
    g = _g("1", status="ended", sales_end_date="06/15/2026")
    act, _ = recommendation(g, Thresholds())
    assert act == "send_back"


def test_recommendation_send_back_below_threshold():
    # 20% of prizes left (top tier 1/5) -> below 40% -> send back.
    g = _g("1", status="active", top_prizes_total=5, top_prizes_remaining=1, odds="1:3.5")
    assert recommendation(g, Thresholds())[0] == "send_back"


def test_recommendation_keep_when_healthy():
    g = _g("1", status="active", top_prizes_total=10, top_prizes_remaining=8, odds="1:3.2")
    assert recommendation(g, Thresholds())[0] == "keep"


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
    prev = {"5310": mk("5310", status="active", top_prizes_total=10, top_prizes_remaining=3)}
    cur = {"5310": mk("5310", status="active", top_prizes_total=10, top_prizes_remaining=1)}
    alerts = evaluate(cur, prev, inventory={"5310"}, thresholds=th)
    assert len(alerts) == 1 and alerts[0].kind == "low_prizes" and alerts[0].owned


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


def test_estimate_uses_highest_count_ever_seen():
    # New game seen at 8 -> original estimate 8 -> 100%.
    cur = {"5432": mk("5432", status="active", top_prizes_remaining=8)}
    estimate_top_prize_totals(cur, {})
    assert cur["5432"].top_prizes_total == 8
    assert abs(cur["5432"].top_prize_pct_remaining - 1.0) < 1e-9

    # Next run drops to 2, but the original estimate stays at the prior max (8).
    prev = {"5432": mk("5432", status="active", top_prizes_total=8, top_prizes_remaining=8)}
    cur2 = {"5432": mk("5432", status="active", top_prizes_remaining=2)}
    estimate_top_prize_totals(cur2, prev)
    assert cur2["5432"].top_prizes_total == 8
    assert abs(cur2["5432"].top_prize_pct_remaining - 0.25) < 1e-9
