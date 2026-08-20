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


def test_recommendation_send_back_when_rating_low():
    # Weak odds AND mostly sold through -> low weighted rating -> send back.
    g = _g("1", status="active", top_prizes_total=10, top_prizes_remaining=1, odds="1:4.8")
    act, reason = recommendation(g, Thresholds())
    assert act == "send_back"
    # the reason is written for whoever reads it, not in shorthand
    assert "out of 100" in reason and "keep it" in reason


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


# --- the wording a clerk actually reads --------------------------------------

def test_the_low_prize_trend_is_stated_without_sigma():
    """"cheap prizes -4.0s vs game" is the right unit for the maths and the wrong
    one for a phone."""
    from lottery_tracker.rules import _skew_phrase
    for z in (-2.7, -4.0, -7.0):
        phrase = _skew_phrase(z)
        assert "σ" not in phrase            # "prizes" has a z in it; sigma is the tell
        assert "small prizes" in phrase and "faster" in phrase
    assert _skew_phrase(-7.0) != _skew_phrase(-2.7)      # the size still shows


def test_an_untrustworthy_density_says_why_not_just_noise():
    from lottery_tracker.rules import _density_doubt

    class _Tiny:
        def tier_z_scores(self):
            return [{"value_num": 1_000_000, "original": 5, "remaining": 2,
                     "z": 0.3, "crit": 2.64, "significant": False},
                    {"value_num": 100, "original": 5000, "remaining": 2000,
                     "z": 0.1, "crit": 2.64, "significant": False}]

    assert "5 top prizes" in _density_doubt(_Tiny())
    assert "too few to tell" in _density_doubt(_Tiny())


def test_an_explanation_never_breaks_a_rating():
    from lottery_tracker.rules import _density_doubt

    class _Broken:
        def tier_z_scores(self):
            raise RuntimeError("no data")

    assert "left out of the score" in _density_doubt(_Broken())
