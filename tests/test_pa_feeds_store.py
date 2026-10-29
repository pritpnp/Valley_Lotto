"""Saving the draws, the side-by-side check, the switch, telling the owner once,
the TV feed, and the job itself. SQLite databases in tmp_path; no network, no
Telegram (both stood in for)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import Session

from lottery_tracker import telegram
from lottery_tracker.pa_feeds import __main__ as job
from lottery_tracker.pa_feeds import display, draws, store
from lottery_tracker.pa_feeds.draws import Draw
from lottery_tracker.web.models import DrawCheckRow, DrawResultRow

PA = Path(__file__).parent / "fixtures" / "pa"
FEED = (PA / "game-number-feed_2026-10-28.xml").read_bytes()
OLD = (PA / "old-scraper-lottery_2026-10-28.json").read_bytes()
PAGE = (PA / "Current-Retailers_2026-10-28.html").read_bytes()
PDF = (PA / "Scratch-Offs_Order_10-26-26.pdf").read_bytes()
PDF_URL = ("https://www.palottery.pa.gov/PaLotteryWebSite/media/Retailer-Documents/"
           "Retailer-Tools/Scratch-Offs_Order/Scratch-Offs_Order_10-26-26.pdf")

T0 = datetime(2026, 10, 29, 0, 10, tzinfo=timezone.utc)


@pytest.fixture()
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 'feeds.db'}")
    store.create_tables(e)
    yield e
    e.dispose()


@pytest.fixture()
def db(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture()
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(telegram, "send", lambda text, formatted=False: out.append(text) or True)
    return out


class Resp:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(response=self)


class Web:
    """Stands in for the internet: url -> bytes, a Resp, or an exception."""

    def __init__(self, monkeypatch, pages):
        self.pages, self.asked = dict(pages), []
        monkeypatch.setattr(job.requests, "get", self.get)
        monkeypatch.setattr(job, "RETRY_WAIT", 0)

    def get(self, url, headers=None, timeout=None):
        self.asked.append(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return page if isinstance(page, Resp) else Resp(page)


def save_fixtures(db, now=T0):
    reading = draws.read_feed(FEED)
    store.save_draws(db, reading.draws, now)
    store.remember_feed_order(db, reading.order, now)
    store.save_draws(db, draws.parse_old_scraper(OLD), now)


def rows(db, **kw):
    return db.scalars(select(DrawResultRow).filter_by(**kw)).all()


# --- tables ------------------------------------------------------------------------

def test_only_the_pa_tables_are_created_and_protected(tmp_path, monkeypatch):
    from lottery_tracker.web import migrate
    protected = []
    monkeypatch.setattr(migrate, "enable_rls", lambda e: protected.append(e) or [])
    e = create_engine(f"sqlite:///{tmp_path / 'x.db'}")
    store.create_tables(e)
    assert set(inspect(e).get_table_names()) == {
        "pa_draw_results", "pa_draw_checks", "pa_feed_settings", "pa_order_lists", "pa_order_games"}
    assert protected == [e]
    store.create_tables(e)                              # safe to run every time
    e.dispose()


# --- saving draws --------------------------------------------------------------------

def test_draws_are_saved_once_and_seen_again(db):
    save_fixtures(db, T0)
    save_fixtures(db, T0 + timedelta(minutes=10))
    assert len(rows(db, source="pa_feed")) == 16 and len(rows(db, source="old_scraper")) == 2
    pb = rows(db, source="pa_feed", game_id="12")[0]
    assert store.aware(pb.first_seen_at) == T0
    assert store.aware(pb.last_seen_at) == T0 + timedelta(minutes=10)
    assert pb.numbers == "18 26 49 50 54" and pb.title == "Powerball"
    assert json.loads(pb.extras) == [{"code": "PB", "label": "Powerball", "value": "01"},
                                     {"code": "PP", "label": "Power Play", "value": "03"}]
    assert (pb.jackpot_text, pb.jackpot_amount) == ("$716,000,000", 716000000.0)
    assert pb.raw.startswith('<game id="12">')


def test_a_moving_jackpot_updates_the_same_row(db):
    save_fixtures(db, T0)
    later = FEED.replace(b"$716,000,000", b"$730,000,000").replace(b"$307,200,000", b"$313,100,000")
    notes = store.save_draws(db, draws.parse_feed(later), T0 + timedelta(hours=1))
    assert notes == []
    pb = rows(db, source="pa_feed", game_id="12")
    assert len(pb) == 1
    assert (pb[0].jackpot_text, pb[0].jackpot_amount) == ("$730,000,000", 730000000.0)
    assert (pb[0].cash_value_text, pb[0].cash_value_amount) == ("$313,100,000", 313100000.0)


def test_a_new_draw_is_a_new_row(db):
    save_fixtures(db, T0)
    nxt = FEED.replace(b"<drawdate>10/26/2026</drawdate><numbers>18 26",
                       b"<drawdate>10/28/2026</drawdate><numbers>02 26")
    store.save_draws(db, draws.parse_feed(nxt), T0 + timedelta(days=1))
    assert sorted(r.draw_date for r in rows(db, source="pa_feed", game_id="12")) == \
        ["2026-10-26", "2026-10-28"]


def test_a_draw_that_reads_differently_later_keeps_the_first_reading_and_says_so(db):
    save_fixtures(db, T0)
    changed = FEED.replace(b"18 26 49 50 54 PB: 01", b"18 26 49 50 55 PB: 01")
    notes = store.save_draws(db, draws.parse_feed(changed), T0 + timedelta(minutes=10))
    assert len(notes) == 1 and "Powerball" in notes[0] and "first reading is kept" in notes[0]
    assert rows(db, source="pa_feed", game_id="12")[0].numbers == "18 26 49 50 54"


def test_the_feeds_order_is_remembered(db):
    save_fixtures(db)
    assert store.feed_order(db) == ["31", "27", "32", "28", "33", "29", "34", "30",
                                    "7", "8", "11", "12", "15", "37", "36", "38"]


# --- telling the owner, once -------------------------------------------------------------

def test_the_same_problem_twice_is_one_message_and_a_fixed_note_when_it_clears(db, sent):
    assert store.report(db, "numbers_format", "⚠️ broken", T0)
    assert not store.report(db, "numbers_format", "⚠️ broken", T0 + timedelta(minutes=10))
    assert sent == ["⚠️ broken"]
    assert store.report(db, "numbers_format", None, T0 + timedelta(minutes=20))
    assert sent[1] == store.FIXED["numbers_format"] and sent[1].startswith("✅")
    assert not store.report(db, "numbers_format", None, T0 + timedelta(minutes=30))
    assert len(sent) == 2


def test_a_different_problem_is_a_new_message(db, sent):
    store.report(db, "numbers_format", "⚠️ one thing", T0)
    store.report(db, "numbers_format", "⚠️ another thing", T0)
    store.report(db, "numbers_format", "⚠️ one thing", T0)       # not "twice in a row"
    assert len(sent) == 3


def test_problems_in_different_places_are_kept_apart(db, sent):
    store.report(db, "numbers_format", "⚠️ x", T0)
    store.report(db, "orders_format", "⚠️ x", T0)
    store.report(db, "orders_format", None, T0)
    assert len(sent) == 3 and "order list" in sent[2]
    store.report(db, "numbers_format", "⚠️ x", T0)
    assert len(sent) == 3


def test_a_quiet_slot_clears_without_a_note(db, sent):
    store.report(db, "orders_check", "⚠️ worth a look", T0)
    store.report(db, "orders_check", None, T0)
    assert sent == ["⚠️ worth a look"]


def test_a_message_telegram_didnt_take_is_tried_again(db, monkeypatch):
    tries = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(telegram, "send", lambda text, formatted=False: tries.append(text) and False)
    store.report(db, "numbers_format", "⚠️ broken", T0)
    monkeypatch.setattr(telegram, "send", lambda text, formatted=False: tries.append(text) or True)
    store.report(db, "numbers_format", "⚠️ broken", T0 + timedelta(minutes=10))
    store.report(db, "numbers_format", "⚠️ broken", T0 + timedelta(minutes=20))
    assert len(tries) == 2


def test_messages_are_html_with_pa_text_escaped(db, sent):
    store.report(db, "numbers_format",
                 store.problem_list("PA's number feed needs a look", ["Xs & Os: <odd>"]), T0)
    assert sent == ["⚠️ <b>PA's number feed needs a look</b>\n• Xs &amp; Os: &lt;odd&gt;"]


def test_a_fetch_failure_is_only_reported_after_an_hour(db, sent):
    for minutes in (0, 10, 30, 60):
        store.fetch_failed(db, "numbers", "PA's number feed", "ConnectTimeout", T0 + timedelta(minutes=minutes))
    assert sent == []
    store.fetch_failed(db, "numbers", "PA's number feed", "ConnectTimeout", T0 + timedelta(minutes=61))
    store.fetch_failed(db, "numbers", "PA's number feed", "the site answered 503", T0 + timedelta(minutes=71))
    assert len(sent) == 1
    assert "PA's number feed" in sent[0] and "Oct 28, 8:10 PM" in sent[0]   # Eastern time
    store.fetch_worked(db, "numbers", T0 + timedelta(minutes=80))
    assert sent[1] == store.FIXED["numbers_fetch"]


def test_a_short_blip_is_never_mentioned(db, sent):
    store.fetch_failed(db, "numbers", "PA's number feed", "ConnectTimeout", T0)
    store.fetch_worked(db, "numbers", T0 + timedelta(minutes=10))
    store.fetch_failed(db, "numbers", "PA's number feed", "ConnectTimeout", T0 + timedelta(minutes=70))
    store.fetch_worked(db, "numbers", T0 + timedelta(minutes=80))
    assert sent == []                         # the clock restarted after the success


# --- comparing the two sources -------------------------------------------------------------

def test_the_real_draws_match(db, sent):
    save_fixtures(db)
    result = store.compare_and_switch(db, T0)
    assert result == {"checked": 2, "mismatches": 0, "switched": False}
    checks = {c.game_id: c for c in db.scalars(select(DrawCheckRow))}
    assert checks["12"].draw_date == "2026-10-26" and checks["12"].matched
    assert checks["15"].draw_date == "2026-10-27" and checks["15"].matched
    assert checks["12"].detail == "Same numbers."     # $716,000,000 == $716 Million
    assert store.get_setting(db, store.TRIAL_STARTED) == T0.isoformat()
    assert sent == []
    # compared once only
    assert store.compare_and_switch(db, T0 + timedelta(minutes=10))["checked"] == 0


def _pb(source, date="2026-10-26", numbers="18 26 49 50 54", bonus="01", jackpot=716000000.0,
        jackpot_text="$716,000,000", next_date="2026-10-28"):
    return Draw(game_id="12", title="Powerball", draw_date=date, numbers=numbers.split(),
                extras=[{"code": "PB", "label": "Powerball", "value": bonus}],
                jackpot_date=next_date, jackpot_text=jackpot_text, jackpot_amount=jackpot,
                source=source)


def _mm(source, date, numbers="14 19 21 45 67", bonus="03"):
    return Draw(game_id="15", title="Mega Millions", draw_date=date, numbers=numbers.split(),
                extras=[{"code": "MB", "label": "Mega Ball", "value": bonus}], source=source)


def test_numbers_are_compared_as_numbers(db, sent):
    store.save_draws(db, [_pb("pa_feed", numbers="08 26 49 50 54", bonus="01")], T0)
    store.save_draws(db, [_pb("old_scraper", numbers="8 26 49 50 54", bonus="1")], T0)
    store.compare_and_switch(db, T0)
    assert db.scalar(select(DrawCheckRow)).matched


def test_jackpot_and_next_draw_are_noted_but_dont_decide(db, sent):
    store.save_draws(db, [_pb("pa_feed", jackpot=730000000.0, jackpot_text="$730,000,000")], T0)
    store.save_draws(db, [_pb("old_scraper", jackpot=716000000.0, jackpot_text="$716 Million",
                              next_date="2026-10-29")], T0)
    store.compare_and_switch(db, T0)
    check = db.scalar(select(DrawCheckRow))
    assert check.matched
    assert "$730,000,000" in check.detail and "$716 Million" in check.detail
    assert "2026-10-29" in check.detail
    assert sent == []


@pytest.mark.parametrize("numbers,bonus,what", [
    ("18 26 49 50 55", "01", "Numbers"),
    ("26 18 49 50 54", "01", "Numbers"),          # same numbers, different order
    ("18 26 49 50 54", "02", "Powerball"),
])
def test_a_mismatch_is_reported_at_once_with_both_readings(db, sent, numbers, bonus, what):
    store.save_draws(db, [_pb("pa_feed")], T0)
    store.save_draws(db, [_pb("old_scraper", numbers=numbers, bonus=bonus)], T0)
    result = store.compare_and_switch(db, T0)
    assert result["mismatches"] == 1
    check = db.scalar(select(DrawCheckRow))
    assert not check.matched and check.detail.startswith(what)
    assert len(sent) == 1
    assert "Powerball" in sent[0] and "Mon Oct 26, 2026" in sent[0]
    assert "PA's feed: 18 26 49 50 54 · Powerball 01" in sent[0]
    assert f"Old scraper: {numbers} · Powerball {bonus}" in sent[0]
    assert "starts over" in sent[0]


def test_a_missing_bonus_ball_is_a_mismatch(db, sent):
    pa = _pb("pa_feed")
    pa.extras = []
    store.save_draws(db, [pa], T0)
    store.save_draws(db, [_pb("old_scraper")], T0)
    store.compare_and_switch(db, T0)
    assert not db.scalar(select(DrawCheckRow)).matched


def test_one_source_alone_starts_nothing(db, sent):
    store.save_draws(db, draws.parse_feed(FEED), T0)
    store.compare_and_switch(db, T0)
    assert store.get_setting(db, store.TRIAL_STARTED) is None
    assert db.scalar(select(func.count()).select_from(DrawCheckRow)) == 0


# --- the 14-day switch ------------------------------------------------------------------------

START = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)      # a Monday
PB_DAYS, MM_DAYS = {0, 2, 5}, {1, 4}                          # Mon Wed Sat / Tue Fri


def run_days(db, first, last, *, mismatch_on=(), skip_mm=(), only=None,
             pa=True, old=True, advise=False):
    """Each day from ``first`` to ``last`` (days after START): that day's real
    draws from both sources (``pa``/``old`` False: that source sent nothing),
    then the check, and with ``advise`` the old scraper's on/off advice.
    Returns the day it switched on."""
    switched_on = None
    for n in range(first, last + 1):
        now = START + timedelta(days=n)
        date = now.date().isoformat()
        both = []
        if now.weekday() in PB_DAYS and only in (None, "12"):
            both.append(("12", date))
        if now.weekday() in MM_DAYS and n not in skip_mm and only in (None, "15"):
            both.append(("15", date))
        for game, d in both:
            make = (lambda s, **kw: _pb(s, date=d, **kw)) if game == "12" else \
                   (lambda s, **kw: _mm(s, d, **kw))
            if pa:
                store.save_draws(db, [make("pa_feed")], now)
            if old:
                store.save_draws(db, [make("old_scraper", numbers="01 02 03 04 05")
                                      if n in mismatch_on else make("old_scraper")], now)
        if store.compare_and_switch(db, now)["switched"] and switched_on is None:
            switched_on = n
        if advise:
            store.advise_old_scraper(db, now)
    return switched_on


def test_it_switches_after_14_days_of_matches_and_not_before(db, sent):
    assert run_days(db, 0, 13) is None
    assert store.pbmm_source(db) == "old_scraper"
    # 13 days 23 hours: still no
    store.compare_and_switch(db, START + timedelta(days=13, hours=23, minutes=59))
    assert store.pbmm_source(db) == "old_scraper"
    assert run_days(db, 14, 14) == 14
    assert store.pbmm_source(db) == "pa_feed"
    assert store.get_setting(db, store.SWITCHED_AT) == (START + timedelta(days=14)).isoformat()
    assert len(sent) == 1 and sent[0].startswith("✅") and "PA's own feed" in sent[0]


def test_the_switch_waits_for_4_draws_of_each_game(db, sent):
    # Powerball every draw; Mega Millions only three times in three weeks
    mm_days = [n for n in range(22) if (START + timedelta(days=n)).weekday() in MM_DAYS]
    assert run_days(db, 0, 21, skip_mm=mm_days[3:]) is None
    _, counts = store.trial_passed(db, START + timedelta(days=21))
    assert counts["15"] == 3 and counts["12"] >= 4
    assert store.pbmm_source(db) == "old_scraper"
    # the 4th Mega Millions match is what tips it
    assert run_days(db, 22, 25) == 22 + next(i for i in range(4)
                                             if (START + timedelta(days=22 + i)).weekday() in MM_DAYS)


def test_a_mismatch_restarts_the_trial(db, sent):
    assert run_days(db, 0, 7, mismatch_on={7}) is None             # Mon day 7: Powerball differs
    assert store.get_setting(db, store.TRIAL_STARTED) == (START + timedelta(days=7)).isoformat()
    assert len(sent) == 1 and "disagree" in sent[0]
    assert run_days(db, 8, 20) is None                              # 14 days from day 0 isn't enough
    assert run_days(db, 21, 21) == 21                               # 14 days from day 7 is
    assert sent[-1].startswith("✅")


def test_a_mismatch_inside_the_trial_blocks_the_switch_even_if_the_reset_was_missed(db, sent):
    run_days(db, 0, 3)
    db.add(DrawCheckRow(game_id="12", draw_date="2026-10-01", matched=False, detail="x",
                        checked_at=START + timedelta(days=3)))
    db.flush()
    run_days(db, 4, 20)
    assert store.pbmm_source(db) == "old_scraper"


def test_it_never_switches_back(db, sent):
    run_days(db, 0, 14)
    assert store.pbmm_source(db) == "pa_feed"
    switched_at = store.get_setting(db, store.SWITCHED_AT)
    run_days(db, 15, 16, mismatch_on={16})                          # old data still arrives, and differs
    assert store.pbmm_source(db) == "pa_feed"
    assert store.get_setting(db, store.SWITCHED_AT) == switched_at
    assert "disagree" in sent[-1] and "already switched" in sent[-1]
    assert sum(m.startswith("✅") for m in sent) == 1                # the switch was told once
    run_days(db, 17, 40)
    assert sum(m.startswith("✅") for m in sent) == 1


def test_the_old_scraper_going_quiet_is_reported_once_during_the_trial(db, sent):
    run_days(db, 0, 2)                                              # last old draw: day 2 (Wed)
    store.check_old_scraper(db, START + timedelta(days=5))
    assert sent == []                                               # 3 days exactly: not yet
    store.check_old_scraper(db, START + timedelta(days=5, minutes=10))
    store.check_old_scraper(db, START + timedelta(days=6))
    assert len(sent) == 1 and "old" in sent[0] and "hasn't sent a new draw" in sent[0]
    run_days(db, 7, 7)                                              # it's back
    store.check_old_scraper(db, START + timedelta(days=7))
    assert sent[-1] == store.FIXED["old_quiet"]


def test_the_old_scraper_going_quiet_after_the_switch_is_expected(db, sent):
    run_days(db, 0, 14)
    n = len(sent)
    store.check_old_scraper(db, START + timedelta(days=30))
    assert len(sent) == n


# --- telling the owner to turn the old scraper OFF, or back ON --------------------------------

OFF_STEPS = ('GitHub → powerball-megamillion → Actions → "Daily Lottery Update" '
             '→ ⋯ (top right) → Disable workflow')
ON_STEPS = OFF_STEPS.replace("Disable", "Enable")


def day(n, **kw):
    return START + timedelta(days=n, **kw)


def test_the_old_scraper_is_running_while_it_shows_newer_draws(db):
    assert not store.old_scraper_running(db, T0)                    # never seen
    store.save_draws(db, draws.parse_old_scraper(OLD), T0)
    assert store.get_setting(db, store.OLD_LAST_NEW) == T0.isoformat()
    store.save_draws(db, draws.parse_old_scraper(OLD), T0 + timedelta(days=2))   # same file again
    assert store.get_setting(db, store.OLD_LAST_NEW) == T0.isoformat()
    assert store.old_scraper_running(db, T0 + timedelta(days=4))
    assert not store.old_scraper_running(db, T0 + timedelta(days=4, minutes=1))
    # an older draw turning up isn't news, and PA's feed doesn't count
    store.save_draws(db, [_pb("old_scraper", date="2026-10-20")], T0 + timedelta(days=5))
    store.save_draws(db, [_pb("pa_feed", date="2026-10-31")], T0 + timedelta(days=5))
    assert not store.old_scraper_running(db, T0 + timedelta(days=5))
    store.save_draws(db, [_pb("old_scraper", date="2026-10-31")], T0 + timedelta(days=5))
    assert store.old_scraper_running(db, T0 + timedelta(days=5))


def test_nothing_is_advised_before_the_switch(db, sent):
    save_fixtures(db)
    store.note_pa_feed(db, False, T0)
    later = T0 + timedelta(days=30)                                 # PA stale, failing, old running
    store.save_draws(db, [_pb("old_scraper", date="2026-11-27")], later)
    assert store.advise_old_scraper(db, later) == {"on": False, "fine": False, "off": False}
    assert sent == []


def test_after_the_switch_turn_it_off_once_a_week_while_it_runs(db, sent):
    assert run_days(db, 0, 14, advise=True) == 14
    assert len(sent) == 2 and sent[0].startswith("✅")
    off = sent[1]
    assert off.startswith("🔌 <b>Turn the old Powerball/Mega Millions scraper OFF</b>")
    assert OFF_STEPS in off
    run_days(db, 15, 20, advise=True)                               # still running: not again yet
    assert len(sent) == 2
    run_days(db, 21, 21, advise=True)                               # a week later: reminded
    assert len(sent) == 3 and sent[2] == off
    # the owner turns it off: its last new draw was day 21, so by day 28 it isn't running
    run_days(db, 22, 40, old=False, advise=True)
    assert len(sent) == 3


def test_no_turn_off_message_once_it_has_stopped(db, sent):
    save_fixtures(db, T0)                                           # the old scraper's last news
    store.set_setting(db, store.PBMM_SOURCE, "pa_feed", T0)
    later = T0 + timedelta(days=4, minutes=10)
    store.save_draws(db, [_pb("pa_feed", date="2026-10-31"), _mm("pa_feed", "2026-10-30")], later)
    assert store.advise_old_scraper(db, later) == {"on": False, "fine": False, "off": False}
    assert sent == []
    store.save_draws(db, [_pb("old_scraper", date="2026-10-31")], later)   # it's back
    assert store.advise_old_scraper(db, later)["off"]
    assert sent[0].startswith("🔌")


def test_pa_falling_behind_says_turn_it_back_on_once_per_problem(db, sent):
    run_days(db, 0, 14, advise=True)
    n = len(sent)                                                   # the switch, then "OFF"
    # PA's feed stops; the old scraper keeps going. PA's newest Mega Millions is
    # Fri day 11, its newest Powerball Mon day 14.
    run_days(db, 15, 15, pa=False, advise=True)                     # Mega Millions 4 days old: fine
    assert len(sent) == n
    run_days(db, 16, 16, pa=False, advise=True)                     # 5 days: draws missed
    assert len(sent) == n + 1
    on = sent[-1]
    assert on.startswith("⚠️ <b>Turn the old scraper back ON</b>")
    assert "Mega Millions draw is from Fri Nov 13, 2026, 5 days ago" in on
    assert "Powerball" not in on.split("\n")[2]
    assert ON_STEPS in on and "leave it on" in on
    run_days(db, 17, 18, pa=False, advise=True)                     # same problem: not again
    assert len(sent) == n + 1
    run_days(db, 19, 19, pa=False, advise=True)                     # now Powerball too: new problem
    assert len(sent) == n + 2 and "Powerball draw is from Mon Nov 16, 2026" in sent[-1]

    # the TVs show the old scraper's newer draws meanwhile
    games = {g["id"]: g for g in display.build_payload(db, now=day(19))["games"]}
    assert (games["12"]["source"], games["12"]["draw_date"]) == ("old_scraper", "2026-11-21")
    assert (games["15"]["source"], games["15"]["draw_date"]) == ("old_scraper", "2026-11-20")

    run_days(db, 20, 21, advise=True)                               # PA's Powerball is back,
    assert len(sent) == n + 2                                       # Mega Millions not yet: quiet
    run_days(db, 22, 22, advise=True)                               # all caught up
    assert len(sent) == n + 3
    assert sent[-1] == store.FIXED["old_on"]
    assert sent[-1].startswith("✅ PA's feed is fine again, you can turn the old scraper OFF again")
    assert OFF_STEPS in sent[-1]
    games = {g["id"]: g for g in display.build_payload(db, now=day(22))["games"]}
    assert games["12"]["source"] == games["15"]["source"] == "pa_feed"

    run_days(db, 23, 28, advise=True)                               # the weekly reminder restarts
    assert len(sent) == n + 3                                       # from the "fine again" note
    run_days(db, 29, 29, advise=True)
    assert sent[-1].startswith("🔌")


def test_a_problem_that_clears_and_comes_back_is_news_again(db, sent):
    run_days(db, 0, 14, advise=True)
    n = len(sent)
    run_days(db, 15, 19, pa=False, advise=True)                     # PA stops: Mega Millions late,
    assert len(sent) == n + 2                                       # then Powerball late too
    # PA's Powerball comes back but no Mega Millions draw arrives: still a
    # problem, nothing new to say
    run_days(db, 20, 26, only="12", advise=True)
    assert len(sent) == n + 2
    # Powerball falls behind again: that's news again
    run_days(db, 27, 31, pa=False, advise=True)
    assert len(sent) == n + 3
    assert "Powerball draw is from Sat Nov 28, 2026" in sent[-1] and "Mega Millions" in sent[-1]


def test_a_mismatch_after_the_switch_says_turn_it_back_on(db, sent):
    run_days(db, 0, 14, advise=True)
    n = len(sent)
    run_days(db, 15, 16, mismatch_on={16}, advise=True)             # Wed day 16: Powerball differs
    assert len(sent) == n + 2
    assert "disagree" in sent[-2] and "already switched" in sent[-2]
    assert sent[-1].startswith("⚠️ <b>Turn the old scraper back ON</b>")
    assert "disagree on Powerball, draw of Wed Nov 18, 2026" in sent[-1]
    run_days(db, 17, 18, advise=True)                               # Mega Millions matching
    assert len(sent) == n + 2                                       # doesn't clear Powerball's
    run_days(db, 19, 19, advise=True)                               # Sat: Powerball matches again
    assert sent[-1] == store.FIXED["old_on"]
    assert store.pbmm_source(db) == "pa_feed"                       # never switched back


def test_a_turn_on_message_telegram_didnt_take_is_tried_again(db, monkeypatch):
    tries = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setattr(telegram, "send", lambda text, formatted=False: tries.append(text) or True)
    run_days(db, 0, 14, advise=True)
    monkeypatch.setattr(telegram, "send", lambda text, formatted=False: tries.append(text) and False)
    run_days(db, 15, 16, pa=False, advise=True)
    monkeypatch.setattr(telegram, "send", lambda text, formatted=False: tries.append(text) or True)
    run_days(db, 17, 18, pa=False, advise=True)
    on = [t for t in tries if "back ON" in t]
    assert len(on) == 2                                             # failed once, then delivered


# --- the TV feed ------------------------------------------------------------------------------

SPEC_POWERBALL = {
    "id": "12",
    "title": "Powerball",
    "position": 11,
    "draw_date": "2026-10-26",
    "numbers": ["18", "26", "49", "50", "54"],
    "extras": [{"code": "PB", "label": "Powerball", "value": "01"},
               {"code": "PP", "label": "Power Play", "value": "03"}],
    "next_draw_date": "2026-10-28",
    "jackpot": {"text": "$716,000,000", "amount": 716000000},
    "cash_value": {"text": "$307,200,000", "amount": 307200000},
    "source": "pa_feed",
    "seen_at": "2026-10-29T00:10:00Z",
}


def test_the_tv_feed_after_the_switch_is_exactly_the_spec(db):
    save_fixtures(db, T0)
    store.set_setting(db, store.PBMM_SOURCE, "pa_feed", T0)
    payload = display.build_payload(db, now=datetime(2026, 10, 29, 0, 20, tzinfo=timezone.utc))
    assert set(payload) == {"generated_at", "pbmm_source", "games"}
    assert payload["generated_at"] == "2026-10-29T00:20:00Z"
    assert payload["pbmm_source"] == "pa_feed"
    assert payload["games"][11] == SPEC_POWERBALL
    json.dumps(payload)                                            # plain JSON


def test_the_tv_feed_has_every_game_in_pas_order(db):
    save_fixtures(db)
    games = display.build_payload(db, now=T0)["games"]
    assert [g["id"] for g in games] == store.feed_order(db)
    assert [g["position"] for g in games] == list(range(16))
    assert [g["title"] for g in games][:3] == ["PICK 2 (Day)", "PICK 2 (Evening)", "PICK 3 (Day)"]
    for g in games:
        assert set(g) == set(SPEC_POWERBALL)
    by_id = {g["id"]: g for g in games}
    assert by_id["31"]["next_draw_date"] is None and by_id["31"]["jackpot"] is None
    assert by_id["31"]["extras"] == [{"code": "WB", "label": "Wild Ball", "value": "7"}]
    assert by_id["36"]["jackpot"] is None
    assert by_id["36"]["cash_value"] == {"text": "$10,000,000", "amount": 10000000}
    assert by_id["37"]["jackpot"] == {"text": "$1,000,000 A Year For Life", "amount": None}
    assert by_id["38"]["title"] == "Powerball Xs & Os"
    assert by_id["7"]["jackpot"] == {"text": "$52,000", "amount": 52000}


def test_before_the_switch_powerball_and_mega_millions_come_from_the_old_scraper(db):
    save_fixtures(db)
    store.compare_and_switch(db, T0)                               # both matched
    payload = display.build_payload(db, now=T0)
    assert payload["pbmm_source"] == "old_scraper"
    pb = payload["games"][11]
    assert pb == {
        "id": "12", "title": "Powerball", "position": 11, "draw_date": "2026-10-26",
        "numbers": ["18", "26", "49", "50", "54"],
        # the bonus ball from the old scraper; Power Play added because PA's matched
        "extras": [{"code": "PB", "label": "Powerball", "value": "01"},
                   {"code": "PP", "label": "Power Play", "value": "03"}],
        "next_draw_date": "2026-10-28",
        "jackpot": {"text": "$716 Million", "amount": 716000000},
        "cash_value": {"text": "$307,200,000", "amount": 307200000},
        "source": "old_scraper",
        "seen_at": "2026-10-29T00:10:00Z",
    }
    mm = payload["games"][12]
    assert mm["source"] == "old_scraper" and mm["jackpot"]["text"] == "$463 Million"
    assert mm["cash_value"] == {"text": "$194,900,000", "amount": 194900000}
    assert payload["games"][14]["source"] == "pa_feed"            # Double Play is PA's own


def test_before_the_switch_pa_extras_need_a_matched_reading_of_that_draw(db):
    save_fixtures(db)
    pb = display.build_payload(db, now=T0)["games"][11]           # not compared yet
    assert pb["source"] == "old_scraper"
    assert pb["extras"] == [{"code": "PB", "label": "Powerball", "value": "01"}]
    assert pb["cash_value"] is None


def test_before_the_switch_a_mismatched_draw_shows_only_the_old_scraper(db, sent):
    store.save_draws(db, draws.parse_feed(FEED), T0)
    store.remember_feed_order(db, draws.read_feed(FEED).order, T0)
    store.save_draws(db, draws.parse_old_scraper(OLD.replace(b"\n      1\n", b"\n      2\n")), T0)
    store.compare_and_switch(db, T0)
    pb = display.build_payload(db, now=T0)["games"][11]
    assert pb["extras"] == [{"code": "PB", "label": "Powerball", "value": "02"}]
    assert pb["cash_value"] is None


def test_before_the_switch_the_old_scrapers_latest_draw_wins_even_if_pa_is_ahead(db):
    save_fixtures(db)
    store.compare_and_switch(db, T0)
    newer = FEED.replace(b"<drawdate>10/26/2026</drawdate><numbers>18 26 49 50 54 PB: 01",
                         b"<drawdate>10/28/2026</drawdate><numbers>01 02 03 04 05 PB: 06")
    store.save_draws(db, draws.parse_feed(newer), T0 + timedelta(hours=1))
    pb = display.build_payload(db, now=T0)["games"][11]
    assert pb["draw_date"] == "2026-10-26" and pb["source"] == "old_scraper"
    # after the switch: PA's feed only
    store.set_setting(db, store.PBMM_SOURCE, "pa_feed", T0)
    pb = display.build_payload(db, now=T0)["games"][11]
    assert pb["draw_date"] == "2026-10-28" and pb["numbers"] == ["01", "02", "03", "04", "05"]
    assert pb["source"] == "pa_feed"


def test_after_the_switch_the_old_scrapers_same_or_older_draw_is_ignored(db):
    save_fixtures(db)                                              # same draws from both
    store.set_setting(db, store.PBMM_SOURCE, "pa_feed", T0)
    store.compare_and_switch(db, T0)
    store.save_draws(db, [_pb("old_scraper", date="2026-10-24", numbers="01 02 03 04 05")], T0)
    games = display.build_payload(db, now=T0)["games"]
    assert games[11] == SPEC_POWERBALL
    assert games[12]["source"] == "pa_feed" and games[12]["jackpot"]["text"] == "$463,000,000"


def test_after_the_switch_a_newer_old_scraper_draw_shows_until_pa_catches_up(db):
    """PA's feed fell behind and the owner turned the old scraper back on."""
    save_fixtures(db)
    store.set_setting(db, store.PBMM_SOURCE, "pa_feed", T0)
    later = T0 + timedelta(days=1)
    store.save_draws(db, [_pb("old_scraper", date="2026-10-28", numbers="02 03 04 05 06",
                              bonus="07", jackpot=730000000.0, jackpot_text="$730 Million",
                              next_date="2026-10-31")], later)
    payload = display.build_payload(db, now=later)
    assert payload["pbmm_source"] == "pa_feed"
    assert payload["games"][11] == {
        "id": "12", "title": "Powerball", "position": 11, "draw_date": "2026-10-28",
        "numbers": ["02", "03", "04", "05", "06"],
        "extras": [{"code": "PB", "label": "Powerball", "value": "07"}],   # nothing of PA's
        "next_draw_date": "2026-10-31",
        "jackpot": {"text": "$730 Million", "amount": 730000000},
        "cash_value": None,
        "source": "old_scraper",
        "seen_at": "2026-10-30T00:10:00Z",
    }
    assert payload["games"][12]["source"] == "pa_feed"            # Mega Millions: PA's is as new

    caught_up = FEED.replace(b"<drawdate>10/26/2026</drawdate><numbers>18 26 49 50 54 PB: 01",
                             b"<drawdate>10/28/2026</drawdate><numbers>02 03 04 05 06 PB: 07")
    store.save_draws(db, draws.parse_feed(caught_up), later + timedelta(minutes=10))
    pb = display.build_payload(db, now=later)["games"][11]
    assert pb["source"] == "pa_feed" and pb["draw_date"] == "2026-10-28"
    assert pb["extras"][1] == {"code": "PP", "label": "Power Play", "value": "03"}


def test_after_the_switch_the_old_scraper_fills_in_a_game_pa_never_had(db):
    store.save_draws(db, draws.parse_old_scraper(OLD), T0)
    store.set_setting(db, store.PBMM_SOURCE, "pa_feed", T0)
    assert [g["source"] for g in display.build_payload(db, now=T0)["games"]] == \
        ["old_scraper", "old_scraper"]


def test_a_game_pa_adds_shows_up_by_itself(db):
    extra = FEED.replace(b"</games>", b'<game id="40"><title>Cash Pop</title><drawdate>10/28/2026'
                                      b'</drawdate><numbers>07</numbers><jackpotdate /><jackpotamount />'
                                      b'<cashvalue /></game></games>')
    reading = draws.read_feed(extra)
    store.save_draws(db, reading.draws, T0)
    store.remember_feed_order(db, reading.order, T0)
    games = display.build_payload(db, now=T0)["games"]
    assert len(games) == 17 and games[16]["title"] == "Cash Pop" and games[16]["position"] == 16


def test_a_game_that_cant_be_read_keeps_its_last_good_draw_on_the_tvs(db):
    save_fixtures(db)
    broken = FEED.replace(b"05 11 15 18 35", b"05 11 15 18 3S")       # Cash 5
    reading = draws.read_feed(broken)
    store.save_draws(db, reading.draws, T0 + timedelta(minutes=10))
    store.remember_feed_order(db, reading.order, T0 + timedelta(minutes=10))
    games = display.build_payload(db, now=T0)["games"]
    assert len(games) == 16 and games[9]["title"] == "Cash 5"
    assert games[9]["numbers"] == ["05", "11", "15", "18", "35"]


def test_an_empty_database_is_an_empty_feed(db):
    payload = display.build_payload(db, now=T0)
    assert payload == {"generated_at": "2026-10-29T00:10:00Z", "pbmm_source": "old_scraper",
                       "games": []}


def test_the_old_scraper_shows_before_pas_feed_was_ever_read(db):
    store.save_draws(db, draws.parse_old_scraper(OLD), T0)
    games = display.build_payload(db, now=T0)["games"]
    assert [(g["id"], g["position"], g["source"]) for g in games] == \
        [("12", 0, "old_scraper"), ("15", 1, "old_scraper")]


# --- the job ------------------------------------------------------------------------------------

def test_without_a_database_link_it_says_so_plainly_and_succeeds(monkeypatch, capsys, sent):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    web = Web(monkeypatch, {})
    assert job.run(["numbers"]) == 0
    out = capsys.readouterr().out.strip()
    assert "DATABASE_URL" in out and "GitHub secret" in out
    assert len(out.splitlines()) == 1 and out.endswith(".")
    assert web.asked == [] and sent == []
    monkeypatch.setenv("DATABASE_URL", "  ")
    assert job.run(["orders"]) == 0


def everything(monkeypatch, **override):
    pages = {job.FEED_URL: FEED, job.OLD_SCRAPER_URL: OLD,
             job.RETAILERS_URL: PAGE, PDF_URL: PDF}
    pages.update(override)
    return Web(monkeypatch, pages)


def test_the_numbers_job_saves_compares_and_exits_0(monkeypatch, tmp_path, sent, capsys):
    url = f"sqlite:///{tmp_path / 'job.db'}"
    monkeypatch.setenv("DATABASE_URL", f'"{url}"')                # quotes tolerated
    web = everything(monkeypatch)
    assert job.run(["numbers"], now=T0) == 0
    assert web.asked == [job.FEED_URL, job.OLD_SCRAPER_URL]
    e = create_engine(url)
    with Session(e) as s:
        assert len(rows(s, source="pa_feed")) == 16 and len(rows(s, source="old_scraper")) == 2
        assert s.scalar(select(func.count()).select_from(DrawCheckRow)) == 2
        assert len(display.build_payload(s, now=T0)["games"]) == 16
    e.dispose()
    assert sent == []
    assert "16 games read" in capsys.readouterr().out


def test_all_runs_both(monkeypatch, tmp_path, sent):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'job.db'}")
    web = everything(monkeypatch)
    assert job.run(["all"], now=T0) == 0
    assert web.asked == [job.FEED_URL, job.OLD_SCRAPER_URL, job.RETAILERS_URL, PDF_URL]
    assert len(sent) == 1 and "55 games" in sent[0]


def test_a_broken_feed_is_reported_once_then_fixed(monkeypatch, tmp_path, sent):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'job.db'}")
    web = everything(monkeypatch, **{job.FEED_URL: b"<html>We'll be right back</html>"})
    assert job.run(["numbers"], now=T0) == 0
    assert job.run(["numbers"], now=T0 + timedelta(minutes=10)) == 0
    assert len(sent) == 1 and "nothing from it was saved" in sent[0]
    web.pages[job.FEED_URL] = FEED
    assert job.run(["numbers"], now=T0 + timedelta(minutes=20)) == 0
    assert sent[1] == store.FIXED["numbers_format"]


def test_one_unreadable_game_is_reported_and_the_rest_saved(monkeypatch, tmp_path, sent):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'job.db'}")
    everything(monkeypatch, **{job.FEED_URL: FEED.replace(b"PB: 01 PP: 03", b"PB 01 PP: 03")})
    job.run(["numbers"], now=T0)
    job.run(["numbers"], now=T0 + timedelta(minutes=10))
    assert len(sent) == 1 and "Powerball" in sent[0] and "left out" in sent[0]
    e = create_engine(f"sqlite:///{tmp_path / 'job.db'}")
    with Session(e) as s:
        assert len(rows(s, source="pa_feed")) == 15
    e.dispose()


def test_an_unknown_extras_label_is_reported_once(monkeypatch, tmp_path, sent):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'job.db'}")
    everything(monkeypatch, **{job.FEED_URL: FEED.replace(b"Mill Ball: 03", b"Life Ball: 03")})
    for minutes in (0, 10, 20):
        job.run(["numbers"], now=T0 + timedelta(minutes=minutes))
    assert len(sent) == 1 and "Life Ball" in sent[0]


def test_pa_being_down_is_reported_only_after_an_hour(monkeypatch, tmp_path, sent):
    import requests
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'job.db'}")
    web = everything(monkeypatch, **{job.FEED_URL: requests.ConnectTimeout("slow")})
    for minutes in (0, 10, 30, 50, 60):
        assert job.run(["numbers"], now=T0 + timedelta(minutes=minutes)) == 0
    assert sent == []
    assert web.asked.count(job.FEED_URL) == 10                    # one retry each run
    job.run(["numbers"], now=T0 + timedelta(minutes=70))
    job.run(["numbers"], now=T0 + timedelta(minutes=80))
    assert len(sent) == 1 and "PA's number feed" in sent[0] and "ConnectTimeout" in sent[0]
    web.pages[job.FEED_URL] = FEED
    job.run(["numbers"], now=T0 + timedelta(minutes=90))
    assert sent[1] == store.FIXED["numbers_fetch"]


def test_an_error_page_counts_as_a_failed_fetch(monkeypatch):
    Web(monkeypatch, {"https://x": Resp(b"Service Unavailable", 503)})
    with pytest.raises(job.FetchError, match="503"):
        job.download("https://x")
    Web(monkeypatch, {"https://x": Resp(b"   ")})
    with pytest.raises(job.FetchError, match="empty"):
        job.download("https://x")


def test_one_retry_rescues_a_blip(monkeypatch):
    import requests
    answers = [requests.ConnectionError("blip"), Resp(b"ok")]
    monkeypatch.setattr(job, "RETRY_WAIT", 0)
    monkeypatch.setattr(job.requests, "get", lambda url, headers=None, timeout=None:
                        (lambda a: (_ for _ in ()).throw(a) if isinstance(a, Exception) else a)(answers.pop(0)))
    assert job.download("https://x") == b"ok"


def test_after_the_switch_the_old_scraper_stopping_is_not_a_problem(monkeypatch, tmp_path, sent):
    import requests
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'job.db'}")
    e = create_engine(f"sqlite:///{tmp_path / 'job.db'}")
    store.create_tables(e)
    with Session(e) as s:
        store.set_setting(s, store.PBMM_SOURCE, "pa_feed", T0)
        s.commit()
    e.dispose()
    everything(monkeypatch, **{job.OLD_SCRAPER_URL: requests.HTTPError("404")})
    for hours in range(0, 6):
        job.run(["numbers"], now=T0 + timedelta(hours=hours))
    assert sent == []


def switched_db(url):
    e = create_engine(url)
    store.create_tables(e)
    with Session(e) as s:
        store.set_setting(s, store.PBMM_SOURCE, "pa_feed", T0)
        store.set_setting(s, store.SWITCHED_AT, T0.isoformat(), T0)
        s.commit()
    e.dispose()


@pytest.mark.parametrize("bad", ["down", "not the feed", "Powerball unreadable"])
def test_after_the_switch_pa_failing_for_over_6_hours_says_turn_it_back_on(monkeypatch, tmp_path,
                                                                           sent, bad):
    import requests
    url = f"sqlite:///{tmp_path / 'job.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    switched_db(url)
    web = everything(monkeypatch)
    job.run(["numbers"], now=T0)
    assert len(sent) == 1 and sent[0].startswith("🔌")             # it still runs: turn it OFF
    web.pages[job.FEED_URL] = {
        "down": requests.ConnectTimeout("slow"),
        "not the feed": b"<html>We'll be right back</html>",
        "Powerball unreadable": FEED.replace(b"PB: 01 PP: 03", b"PB 01 PP: 03"),
    }[bad]
    first = T0 + timedelta(minutes=10)
    for minutes in (0, 60, 120, 240, 360):                          # 6 hours exactly: not yet
        assert job.run(["numbers"], now=first + timedelta(minutes=minutes)) == 0
    assert not any("back ON" in m for m in sent)
    job.run(["numbers"], now=first + timedelta(minutes=370))
    job.run(["numbers"], now=first + timedelta(minutes=380))
    on = [m for m in sent if "back ON" in m]
    assert len(on) == 1
    assert "PA's number feed has been failing since Oct 28, 8:20 PM (over 6 hours)" in on[0]
    web.pages[job.FEED_URL] = FEED
    job.run(["numbers"], now=first + timedelta(minutes=390))
    assert sent[-1] == store.FIXED["old_on"]


def test_after_the_switch_a_short_pa_outage_says_nothing_about_the_old_scraper(monkeypatch, tmp_path,
                                                                               sent):
    import requests
    url = f"sqlite:///{tmp_path / 'job.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    switched_db(url)
    web = everything(monkeypatch)
    job.run(["numbers"], now=T0)
    web.pages[job.FEED_URL] = requests.ConnectTimeout("slow")
    for minutes in range(10, 371, 30):                              # failing for 6 hours
        job.run(["numbers"], now=T0 + timedelta(minutes=minutes))
    web.pages[job.FEED_URL] = FEED
    job.run(["numbers"], now=T0 + timedelta(minutes=380))
    assert not any("back ON" in m or "fine again" in m for m in sent)
    assert any("Can't reach PA's number feed" in m for m in sent)   # the outage itself was told


def test_a_dry_run_reads_and_prints_but_saves_and_sends_nothing(monkeypatch, tmp_path, sent, capsys):
    db_file = tmp_path / "job.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_file}")
    monkeypatch.setattr(job, "_known_games", lambda: {})
    everything(monkeypatch)
    assert job.run(["all", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "PA's number feed: 16 games" in out and "Powerball: 2026-10-26  18 26 49 50 54" in out
    assert "Old scraper: 2 games" in out
    assert "as of 2026-10-26: 55 games" in out and "1717 $50 50/pack $5,000,000 LION'S SHARE" in out
    assert not db_file.exists() and sent == []


def test_a_real_bug_still_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'job.db'}")
    everything(monkeypatch)
    monkeypatch.setattr(store, "save_draws", lambda *a, **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        job.run(["numbers"], now=T0)


def test_the_workflow_runs_each_job_on_its_schedule():
    import yaml
    wf = yaml.safe_load(Path(".github/workflows/pa-feeds.yml").read_text())
    on = wf.get("on") or wf.get(True)                              # YAML reads "on" as True
    assert [s["cron"] for s in on["schedule"]] == ["*/10 * * * *", "7 * * * *"]
    assert "workflow_dispatch" in on
    assert wf["concurrency"]["group"] == "pa-feeds"
    steps = wf["jobs"]["read"]["steps"]
    install = next(s for s in steps if "pip install" in s.get("run", ""))["run"]
    for req in ("requirements.txt", "requirements-app.txt", "requirements-feeds.txt"):
        assert req in install and Path(req).exists()
    run = next(s for s in steps if "pa_feeds" in s.get("run", ""))
    assert set(run["env"]) >= {"DATABASE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
    assert '"*/10 * * * *") what=numbers' in run["run"] and '"7 * * * *")    what=orders' in run["run"]
    assert "what=all" in run["run"]
    assert "pdfplumber" in Path("requirements-feeds.txt").read_text()
