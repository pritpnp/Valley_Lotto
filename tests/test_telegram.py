"""Telegram: new games, ended games, and failures, without ever leaking the token."""

from lottery_tracker import telegram
from lottery_tracker.model import Game
from lottery_tracker.rules import Alert, Severity

from _games import verified

TOKEN = "123456:SECRET-TOKEN"


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body or {"ok": status == 200}

    def json(self):
        return self._body


def _setup(monkeypatch, responder):
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987")
    import requests

    def post(url, json=None, timeout=None):
        sent.append((url, json))
        return responder(url, json)
    monkeypatch.setattr(requests, "post", post)
    return sent


def test_does_nothing_until_both_settings_exist(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987")
    assert telegram.send("hello") is False


def test_sends_to_the_chat(monkeypatch):
    sent = _setup(monkeypatch, lambda u, j: Resp())
    assert telegram.send("hello") is True
    assert sent[0][1] == {"chat_id": "987", "text": "hello", "disable_web_page_preview": True}


def test_a_refusal_is_reported_without_the_token(monkeypatch, capsys):
    _setup(monkeypatch, lambda u, j: Resp(401, {"description": "Unauthorized"}))
    assert telegram.send("hello") is False
    err = capsys.readouterr().err
    assert "Unauthorized" in err and TOKEN not in err


def test_telegram_being_down_never_stops_the_run(monkeypatch, capsys):
    def boom(u, j):
        raise ConnectionError(f"could not reach {u}")    # the URL contains the token
    _setup(monkeypatch, boom)
    assert telegram.send("hello") is False
    assert TOKEN not in capsys.readouterr().err


def test_long_messages_are_split_on_lines(monkeypatch):
    sent = _setup(monkeypatch, lambda u, j: Resp())
    text = "\n".join(f"line {i} " + "x" * 90 for i in range(200))     # ~20,000 characters
    assert telegram.send(text)
    parts = [j["text"] for _, j in sent]
    assert len(parts) > 1 and all(len(p) <= telegram.LIMIT for p in parts)
    assert "".join(parts) == text


def test_new_and_ended_games_are_announced_plainly():
    new = verified(Game(game_number="1807", name="Lucky Stars", price=5, status="active",
                        odds="1:3.6", top_prize_value="$100,000",
                        prize_tiers=[{"value": "$100,000", "remaining": 6}, {"value": "$50", "remaining": 900}],
                        tier_originals={"100000.0": 6, "50.0": 1000}))
    old = Game(game_number="1766", name="LOVE IS BLIND", price=2, status="ended",
               sales_end_date="09/28/2026", claim_deadline="03/27/2027")
    alerts = [Alert("new", "1807", "Lucky Stars", Severity.INFO, "new"),
              Alert("ended", "1766", "LOVE IS BLIND", Severity.INFO, "ended"),
              Alert("low_prizes", "1700", "Other", Severity.INFO, "low")]
    msg = telegram.game_news(alerts, {"1807": new, "1766": old})
    assert "🆕 New on sale (1)" in msg and "#1807 Lucky Stars" in msg
    assert "$5 ticket" in msg and "wins something 1 in 3.6" in msg
    assert "6 of 6 top prizes ($100,000) left" in msg
    assert "🏁 Sales ended (1)" in msg and "cash winners until 03/27/2027" in msg
    assert "Other" not in msg                      # only new and ended games


def test_nothing_to_say_means_no_message():
    assert telegram.game_news([Alert("low_prizes", "1", "x", Severity.INFO, "m")], {}) == ""


# --- picked-over games --------------------------------------------------------

def _game(num, price, med_left, top_left=1, top=5, odds=3.5, name=None):
    """A game whose medium prizes have `med_left` of 1,000 left."""
    return verified(Game(game_number=num, name=name or f"Game {num}", price=price, status="active",
                         odds=f"1:{odds}",
                         prize_tiers=[{"value": "$10,000", "remaining": top_left},
                                      {"value": "$100", "remaining": med_left}],
                         tier_originals={"10000.0": top, "100.0": 1000, f"{float(price)}": 50000}))


def test_medium_prizes_leave_out_the_top_prize():
    g = _game("1", 5, med_left=150, top_left=0)
    assert abs(g.medium_pct_remaining - 0.15) < 1e-9          # 150 of 1,000
    assert g.top_prize_pair == (0, 5)


def test_a_game_with_its_jackpot_gone_but_prizes_left_is_not_picked_over():
    games = {"1": _game("1", 5, med_left=800, top_left=0), "2": _game("2", 5, med_left=100)}
    assert [g.game_number for g in telegram.picked_over(games)] == ["2"]


def test_the_list_reads_plainly():
    games = {"1": _game("1", 5, med_left=60, name="Goat Load"), "2": _game("2", 5, med_left=900)}
    msg = telegram.low_games_list(games)
    assert "Picked-over games on sale (1)" in msg and "$5 tickets" in msg
    assert "Goat Load (#1): 6% of medium prizes left" in msg
    assert "top prize 1 of 5 left" in msg and "Game 2" not in msg


def test_a_game_is_announced_once_when_it_becomes_picked_over():
    before = {"1": _game("1", 5, med_left=250), "2": _game("2", 5, med_left=100)}
    after = {"1": _game("1", 5, med_left=150), "2": _game("2", 5, med_left=90)}
    assert [g.game_number for g in telegram.newly_picked_over(after, before)] == ["1"]
    msg = telegram.game_news([], after, before)
    assert "Now picked over (1)" in msg and "Game 1" in msg and "Game 2" not in msg
    assert telegram.game_news([], after, after) == ""          # nothing new, no message
