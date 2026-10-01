"""Each SEND BACK gets at most one replacement, and no two share one.

Within a price, the worst game gets the best replacement, the next-worst the
next-best, and the rest get "nothing better at this price".
"""

from lottery_tracker.model import Game
from lottery_tracker.rules import RatingWeights, Thresholds
from lottery_app.pa_data import Catalog, store_rows
from _games import verified


def _poor(num, price, left):
    """A picked-over game: only `left` of 100 cheap prizes remain."""
    return verified(Game(game_number=num, price=price, status="active", odds="1:4.9",
                         prize_tiers=[{"value": f"${price}", "remaining": left}],
                         tier_originals={f"{float(price)}": 100}))


def _fresh(num, price, odds):
    """A fresh game worth bringing in; better odds rate higher."""
    return verified(Game(game_number=num, price=price, status="active", odds=odds,
                         prize_tiers=[{"value": "$100", "remaining": 9},
                                      {"value": f"${price}", "remaining": 9000}],
                         tier_originals={"100.0": 10, f"{float(price)}": 10000}))


def _swaps(games, carried):
    rows = store_rows(Catalog(games={g.game_number: g for g in games}), set(carried),
                      Thresholds(), RatingWeights())
    by = {r["game_number"]: r for r in rows}
    return {n: [s["game_number"] for s in by[n]["swap_to"]] for n in carried}, by


def test_one_replacement_two_send_backs_goes_to_the_worse_game():
    # GOLD FISH / 5 Star Wins, both $1, only A Latte Money to offer.
    s, by = _swaps([_poor("worse", 1, 2), _poor("bad", 1, 30), _fresh("latte", 1, "1:3.2")],
                   ["worse", "bad"])
    assert by["worse"]["rating"] < by["bad"]["rating"]
    assert s == {"worse": ["latte"], "bad": []}


def test_two_replacements_two_send_backs_one_each_best_to_worst():
    # Extreme Green / Code Word, both $10, two games to offer.
    s, by = _swaps([_poor("worse", 10, 2), _poor("bad", 10, 30),
                    _fresh("best", 10, "1:3.0"), _fresh("good", 10, "1:3.6")],
                   ["worse", "bad"])
    assert by["worse"]["rating"] < by["bad"]["rating"]
    assert s == {"worse": ["best"], "bad": ["good"]}


def test_more_send_backs_than_replacements_the_worst_get_them():
    s, _ = _swaps([_poor("w1", 5, 1), _poor("w2", 5, 10), _poor("w3", 5, 30),
                   _fresh("best", 5, "1:3.0"), _fresh("good", 5, "1:3.6")],
                  ["w1", "w2", "w3"])
    assert s == {"w1": ["best"], "w2": ["good"], "w3": []}


def test_a_single_send_back_gets_the_best_replacement_only():
    s, _ = _swaps([_poor("only", 20, 2), _fresh("best", 20, "1:3.0"), _fresh("good", 20, "1:3.6")],
                  ["only"])
    assert s == {"only": ["best"]}


def test_prices_are_matched_separately_and_keeps_get_nothing():
    s, by = _swaps([_poor("p1", 1, 2), _poor("p10", 10, 2), _fresh("keep1", 1, "1:3.0"),
                    _fresh("new1", 1, "1:3.1"), _fresh("new10", 10, "1:3.1")],
                   ["p1", "p10", "keep1"])
    assert by["keep1"]["action"] == "keep" and s["keep1"] == []
    assert s["p1"] == ["new1"] and s["p10"] == ["new10"]
