import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lottery_tracker.model import Game, merge_games  # noqa: E402
from lottery_tracker.rules import Thresholds, evaluate  # noqa: E402


def mk(num, **kw):
    return Game(game_number=num, **kw)


def test_owned_game_ending_is_critical():
    prev = {"5432": mk("5432", name="Big Money", status="active")}
    cur = {"5432": mk("5432", name="Big Money", status="ended", claim_deadline="08/14/2026")}
    alerts = evaluate(cur, prev, inventory={"5432"}, thresholds=Thresholds())
    assert len(alerts) == 1
    a = alerts[0]
    assert a.kind == "ended" and a.owned and a.severity.value == "critical"
    assert "08/14/2026" in a.message


def test_ended_only_alerts_once():
    prev = {"5432": mk("5432", status="ended")}
    cur = {"5432": mk("5432", status="ended")}
    alerts = evaluate(cur, prev, inventory={"5432"}, thresholds=Thresholds())
    assert alerts == []


def test_low_prize_transition_for_owned_game():
    th = Thresholds(top_prize_pct=0.25, top_prize_count_floor=1, total_prize_pct=None)
    prev = {"5310": mk("5310", status="active", top_prizes_total=10, top_prizes_remaining=3)}
    cur = {"5310": mk("5310", status="active", top_prizes_total=10, top_prizes_remaining=1)}
    alerts = evaluate(cur, prev, inventory={"5310"}, thresholds=th)
    assert len(alerts) == 1
    assert alerts[0].kind == "low_prizes" and alerts[0].owned


def test_low_prize_not_repeated():
    th = Thresholds(top_prize_pct=0.25, top_prize_count_floor=1)
    prev = {"5310": mk("5310", status="active", top_prizes_total=10, top_prizes_remaining=1)}
    cur = {"5310": mk("5310", status="active", top_prizes_total=10, top_prizes_remaining=1)}
    assert evaluate(cur, prev, inventory={"5310"}, thresholds=th) == []


def test_non_inventory_low_ignored_unless_report_all():
    th = Thresholds(top_prize_pct=0.25)
    prev = {"5500": mk("5500", status="active", top_prizes_total=20, top_prizes_remaining=18)}
    cur = {"5500": mk("5500", status="active", top_prizes_total=20, top_prizes_remaining=2)}
    assert evaluate(cur, prev, inventory=set(), thresholds=th) == []
    a = evaluate(cur, prev, inventory=set(), thresholds=th, report_all_games=True)
    assert len(a) == 1 and not a[0].owned


def test_owned_game_vanishing_flags_removed():
    prev = {"5310": mk("5310", name="Lucky 7s", status="active")}
    cur = {}  # dropped off all pages
    alerts = evaluate(cur, prev, inventory={"5310"}, thresholds=Thresholds())
    assert len(alerts) == 1 and alerts[0].kind == "removed"


def test_merge_precedence_ended_wins():
    remaining = [mk("5432", name="Big Money", status="active", top_prizes_remaining=5, top_prizes_total=8)]
    active = [mk("5432", name="Big Money", status="active")]
    ended = [mk("5432", name="Big Money", status="ended", claim_deadline="08/14/2026")]
    merged = merge_games(remaining, ended, active)
    g = merged["5432"]
    assert g.status == "ended"            # ended wins
    assert g.top_prizes_remaining == 5    # but prize counts retained
    assert g.claim_deadline == "08/14/2026"
