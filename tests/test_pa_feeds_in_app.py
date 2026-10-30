"""The app reading PA's numbers itself when the TVs ask (pa_feeds/in_app.py):
one run at a time, never making a TV wait, quiet about Telegram, and easy to
turn off. SQLite in tmp_path; the internet, Telegram and the clock stood in for."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from lottery_tracker import telegram
from lottery_tracker.pa_feeds import __main__ as job
from lottery_tracker.pa_feeds import in_app, store
from lottery_tracker.web.app import create_app
from lottery_tracker.web.models import DrawCheckRow, DrawResultRow, FeedSettingRow

PA = Path(__file__).parent / "fixtures" / "pa"
FEED = (PA / "game-number-feed_2026-10-28.xml").read_bytes()
OLD = (PA / "old-scraper-lottery_2026-10-28.json").read_bytes()
T0 = datetime(2026, 10, 29, 0, 10, tzinfo=timezone.utc)


class Slow:
    """A page that only answers once ``go`` is set (at most 10 seconds)."""

    def __init__(self, body):
        self.body, self.go = body, threading.Event()


class Web:
    """Stands in for the internet: url -> bytes, a Slow page, or an exception."""

    def __init__(self, monkeypatch, pages=None):
        self.pages = {job.FEED_URL: FEED, job.OLD_SCRAPER_URL: OLD, **(pages or {})}
        self.asked = []
        monkeypatch.setattr(job.requests, "get", self.get)
        monkeypatch.setattr(job, "RETRY_WAIT", 0)

    def get(self, url, headers=None, timeout=None):
        self.asked.append(url)
        page = self.pages[url]
        if isinstance(page, Slow):
            page.go.wait(10)
            page = page.body
        if isinstance(page, Exception):
            raise page

        class Resp:
            content, status_code = page, 200

            def raise_for_status(self):
                pass
        return Resp()


class Telegram:
    """Stands in for Telegram, set up and working."""

    def __init__(self, monkeypatch):
        self.tried = []
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
        monkeypatch.setattr(telegram, "send", self.send)

    def send(self, text, formatted=False):
        self.tried.append(text)
        return True


class Clock:
    def __init__(self, monkeypatch, now=T0):
        self.now = now
        monkeypatch.setattr(store, "utcnow", lambda: self.now)

    def at(self, **kw):
        self.now = T0 + timedelta(**kw)
        return self.now


@pytest.fixture()
def url(tmp_path):
    return f"sqlite:///{tmp_path / 'app.db'}"


def make_app(url, **config):
    a = create_app({"DATABASE_URL": url, "SECRET_KEY": "k", "DEFAULT_STORE": "t",
                    "SLOTS": "4", "REGISTER_CODE": None, **config})
    a.config.update(TESTING=True)
    return a


@pytest.fixture()
def app(url):
    a = make_app(url, PA_FEED_IN_APP=True)
    yield a
    last = a.extensions["pa_numbers"].thread
    if last is not None:
        last.join(10)                        # nothing left running after the test


def ask(app):
    """One TV check. Waits for a run it started to end. Returns the answer and
    whether it started one."""
    refresher = app.extensions["pa_numbers"]
    before = refresher.thread
    resp = app.test_client().get("/api/numbers")
    started = refresher.thread is not before
    if started:
        refresher.thread.join(10)
        assert not refresher.thread.is_alive()
    return resp, started


def setting(url, key):
    e = create_engine(url)
    with Session(e) as s:
        value = store.get_setting(s, key)
    e.dispose()
    return value


def put_setting(url, key, value, now=T0):
    e = create_engine(url)
    with Session(e) as s:
        store.set_setting(s, key, value, now)
        s.commit()
    e.dispose()


def count(url, what, *where):
    e = create_engine(url)
    with Session(e) as s:
        n = s.scalar(select(func.count()).select_from(what).where(*where))
    e.dispose()
    return n


def waiting(url):
    """The messages waiting for a run that can send them, oldest first."""
    e = create_engine(url)
    with Session(e) as s:
        rows = s.scalars(select(FeedSettingRow).where(FeedSettingRow.key.startswith(store.PENDING))
                         .order_by(FeedSettingRow.updated_at, FeedSettingRow.key)).all()
        out = [r.value for r in rows]
    e.dispose()
    return out


def mismatched_old():
    old = json.loads(OLD)
    old["powerball"]["numbers"][0] = 8              # 08 26 49 50 54; PA has 18
    return json.dumps(old).encode()


# --- a TV's check starts a run when the numbers are over 5 minutes old ----------------------

def test_stale_numbers_are_read_in_the_background_and_shown_at_the_next_check(app, url,
                                                                             monkeypatch):
    web, _ = Web(monkeypatch), Clock(monkeypatch)
    resp, started = ask(app)
    assert resp.status_code == 200 and resp.get_json()["games"] == []   # what was saved
    assert started and web.asked == [job.FEED_URL, job.OLD_SCRAPER_URL]
    assert len(ask(app)[0].get_json()["games"]) == 16
    assert count(url, DrawResultRow, DrawResultRow.source == "pa_feed") == 16
    assert setting(url, store.NUMBERS_LAST_ATTEMPT) == setting(url, store.NUMBERS_LAST_RUN) \
        == T0.isoformat()


def test_many_checks_at_once_start_one_run(app, monkeypatch):
    slow = Slow(FEED)
    web, _ = Web(monkeypatch, {job.FEED_URL: slow}), Clock(monkeypatch)
    runs = []
    numbers = job.numbers
    monkeypatch.setattr(job, "numbers", lambda s, now: runs.append(now) or numbers(s, now))
    gate, answers = threading.Barrier(8), []

    def tv():
        gate.wait(5)
        answers.append(app.test_client().get("/api/numbers").status_code)
    tvs = [threading.Thread(target=tv) for _ in range(8)]
    for t in tvs:
        t.start()
    for t in tvs:
        t.join(10)
    assert answers == [200] * 8
    for _ in range(3):                                   # and more while it waits on PA
        assert not ask(app)[1]
    slow.go.set()
    app.extensions["pa_numbers"].thread.join(10)
    assert len(runs) == 1 and web.asked.count(job.FEED_URL) == 1
    assert not ask(app)[1]                               # it's fresh now


def test_one_run_at_a_time_in_the_app_even_before_the_database_says_so(app, monkeypatch):
    # Checks that come before the running one has saved its start (the
    # database still says it's due) are held back by the app's own lock.
    slow = Slow(FEED)
    Web(monkeypatch, {job.FEED_URL: slow}), Clock(monkeypatch)
    monkeypatch.setattr(store, "numbers_due", lambda session, now: True)
    refresher, threads = app.extensions["pa_numbers"], []
    start = refresher.maybe_start
    monkeypatch.setattr(refresher, "maybe_start", lambda session, now=None: (
        start(session, now) and threads.append(refresher.thread)))
    for _ in range(5):
        assert app.test_client().get("/api/numbers").status_code == 200
    slow.go.set()
    for t in threads:
        t.join(10)
    assert len(threads) == 1
    assert ask(app)[1]                                   # once it's over, the next may start


def test_a_run_in_the_last_5_minutes_starts_none(app, url, monkeypatch):
    web, clock = Web(monkeypatch), Clock(monkeypatch)
    put_setting(url, store.NUMBERS_LAST_RUN, (T0 - timedelta(minutes=4)).isoformat())  # GitHub's
    assert not ask(app)[1]
    clock.at(seconds=59)
    assert not ask(app)[1]
    assert web.asked == []
    clock.at(minutes=1)                                  # 5 minutes after it
    assert ask(app)[1] and web.asked == [job.FEED_URL, job.OLD_SCRAPER_URL]


def test_a_run_started_in_the_last_5_minutes_starts_none_even_if_it_never_finished(app, url,
                                                                                   monkeypatch):
    web, clock = Web(monkeypatch), Clock(monkeypatch)
    put_setting(url, store.NUMBERS_LAST_ATTEMPT, (T0 - timedelta(minutes=2)).isoformat())
    assert not ask(app)[1]
    clock.at(minutes=3)
    assert ask(app)[1]


def test_a_time_far_ahead_of_the_apps_clock_doesnt_stop_it_for_good(app, url, monkeypatch):
    Web(monkeypatch), Clock(monkeypatch)
    put_setting(url, store.NUMBERS_LAST_RUN, (T0 + timedelta(days=3)).isoformat())
    assert ask(app)[1]


def test_a_failing_pa_site_is_tried_again_at_most_every_5_minutes(app, monkeypatch):
    web, clock = Web(monkeypatch, {job.FEED_URL: requests.ConnectTimeout("slow")}), \
        Clock(monkeypatch)
    started = []
    for minute in range(31):                             # a TV checking every minute
        clock.at(minutes=minute)
        if ask(app)[1]:
            started.append(minute)
    assert started == [0, 5, 10, 15, 20, 25, 30]
    assert web.asked.count(job.FEED_URL) == 2 * 7          # one retry each run


def test_the_tv_is_answered_at_once_while_pa_is_slow(app, url, monkeypatch):
    with app.config["SESSION_FACTORY"]() as db:
        db.add(DrawResultRow(source="pa_feed", game_id="8", title="Cash 5",
                             draw_date="2026-10-28", numbers="05 11 15 18 35", extras="[]"))
        db.commit()
    slow = Slow(FEED)
    Web(monkeypatch, {job.FEED_URL: slow}), Clock(monkeypatch)
    began = time.monotonic()
    resp = app.test_client().get("/api/numbers")
    took = time.monotonic() - began
    run = app.extensions["pa_numbers"].thread
    try:
        assert resp.status_code == 200 and took < 3          # PA holds the run up to 10 s
        assert [g["id"] for g in resp.get_json()["games"]] == ["8"]
        assert run is not None and run.is_alive()            # still waiting on PA
    finally:
        slow.go.set()
        run.join(10)
    assert not run.is_alive()
    assert len(ask(app)[0].get_json()["games"]) == 16


# --- never takes the app down -----------------------------------------------------------------

def test_a_run_that_fails_is_logged_and_the_next_one_comes_5_minutes_later(app, url, monkeypatch,
                                                                           caplog):
    web, clock = Web(monkeypatch), Clock(monkeypatch)

    def gone(session, now):
        raise OperationalError("SELECT ...", {}, Exception(
            'connection to server at "db.example.com" failed: password authentication '
            'failed for user "postgres.abc"'))
    monkeypatch.setattr(job, "numbers", gone)
    resp, started = ask(app)
    assert resp.status_code == 200 and started
    assert "OperationalError" in caplog.text
    assert "db.example.com" not in caplog.text and "postgres.abc" not in caplog.text
    assert not app.extensions["pa_numbers"]._running.locked()
    assert setting(url, store.NUMBERS_LAST_ATTEMPT) == T0.isoformat()
    assert setting(url, store.NUMBERS_LAST_RUN) is None
    clock.at(minutes=2)
    assert not ask(app)[1]
    monkeypatch.setattr(job, "numbers", lambda s, now: 1 / 0)       # a bug: the whole story
    clock.at(minutes=5)
    assert ask(app)[1] and "ZeroDivisionError" in caplog.text


def test_a_run_that_fails_before_saving_its_start_still_waits_5_minutes(app, url, monkeypatch,
                                                                        caplog):
    # Supabase made read-only (over its size limit): the run can't save that
    # it started, so the database still says one is due. Without the app's own
    # note of it, every TV check would start a run, and log it failing.
    web, clock = Web(monkeypatch), Clock(monkeypatch)
    started = store.numbers_started

    def read_only(session, now):
        raise OperationalError("UPDATE pa_feed_settings ...", {}, Exception(
            "cannot execute UPDATE in a read-only transaction"))
    monkeypatch.setattr(store, "numbers_started", read_only)
    assert ask(app)[1]
    assert setting(url, store.NUMBERS_LAST_ATTEMPT) is None
    for seconds in (0, 30, 120, 299):                     # TVs and Netlify keep asking
        clock.at(seconds=seconds)
        assert not ask(app)[1]
    assert caplog.text.count("in the background failed") == 1
    assert web.asked == []                                 # PA wasn't asked either
    clock.at(minutes=5)
    assert ask(app)[1]                                     # tried again, 5 minutes on
    assert caplog.text.count("in the background failed") == 2
    monkeypatch.setattr(store, "numbers_started", started)   # Supabase has room again
    clock.at(minutes=10)
    assert ask(app)[1]
    assert setting(url, store.NUMBERS_LAST_RUN) == clock.now.isoformat()
    clock.at(minutes=15)
    assert ask(app)[1]                                     # nothing held over from the failures
    assert caplog.text.count("in the background failed") == 2


def test_a_clerks_save_isnt_held_up_while_the_run_waits_on_the_old_scraper(app, url,
                                                                          monkeypatch):
    # On SQLite (a laptop, or the app with no DATABASE_URL) a run holds the
    # whole database for writing until it saves. PA's numbers are saved before
    # the old scraper's file is asked for, which can take a minute.
    slow = Slow(OLD)
    web, _ = Web(monkeypatch, {job.OLD_SCRAPER_URL: slow}), Clock(monkeypatch)
    app.test_client().get("/api/numbers")
    run = app.extensions["pa_numbers"].thread
    try:
        for _ in range(100):
            if job.OLD_SCRAPER_URL in web.asked:
                break
            time.sleep(0.05)
        assert job.OLD_SCRAPER_URL in web.asked and run.is_alive()
        clerk = create_engine(url, connect_args={"timeout": 1})   # waits 1 s at most
        with clerk.connect() as conn:
            conn.exec_driver_sql("UPDATE stores SET name = name")
            conn.commit()
        clerk.dispose()
        # And the TVs have PA's numbers already.
        assert count(url, DrawResultRow, DrawResultRow.source == "pa_feed") == 16
    finally:
        slow.go.set()
        run.join(10)
    assert setting(url, store.NUMBERS_LAST_RUN) == T0.isoformat()


def test_pa_numbers_are_kept_when_the_rest_of_the_run_fails(app, url, monkeypatch):
    Web(monkeypatch), Clock(monkeypatch)
    monkeypatch.setattr(store, "compare_and_switch", lambda s, now: 1 / 0)
    assert ask(app)[1]
    assert count(url, DrawResultRow, DrawResultRow.source == "pa_feed") == 16
    assert len(app.test_client().get("/api/numbers").get_json()["games"]) == 16


def test_the_app_reads_pa_without_the_pdf_reader(tmp_path):
    # Render's image doesn't install pdfplumber (only the GitHub job needs it),
    # and the free plan's memory is small: the app must never import it, even
    # while it reads PA's numbers itself.
    import os
    import subprocess
    import sys
    code = "\n".join([
        "import sys",
        "sys.modules['pdfplumber'] = None     # not installed, as on Render",
        "from pathlib import Path",
        "from lottery_tracker.pa_feeds import __main__ as job",
        "from lottery_tracker.web.app import create_app",
        "feed, old, db = sys.argv[1:]",
        "pages = {job.FEED_URL: Path(feed).read_bytes(), job.OLD_SCRAPER_URL: Path(old).read_bytes()}",
        "class Resp:",
        "    status_code = 200",
        "    def __init__(self, body): self.content = body",
        "    def raise_for_status(self): pass",
        "job.requests.get = lambda url, headers=None, timeout=None: Resp(pages[url])",
        "app = create_app({'DATABASE_URL': 'sqlite:///' + db, 'SECRET_KEY': 'k',",
        "                  'PA_FEED_IN_APP': True})",
        "assert app.test_client().get('/api/numbers').status_code == 200",
        "app.extensions['pa_numbers'].thread.join(20)",
        "assert len(app.test_client().get('/api/numbers').get_json()['games']) == 16",
        "print('heavy:', [m for m in ('pdfplumber', 'pdfminer', 'PIL', 'bs4') if sys.modules.get(m)])",
    ])
    src = Path(__file__).resolve().parents[1] / "src"
    out = subprocess.run(
        [sys.executable, "-c", code, str(PA / "game-number-feed_2026-10-28.xml"),
         str(PA / "old-scraper-lottery_2026-10-28.json"), str(tmp_path / "r.db")],
        capture_output=True, text=True, timeout=120, env={**os.environ, "PYTHONPATH": str(src)})
    assert out.returncode == 0, out.stderr[-2000:]
    assert "heavy: []" in out.stdout


def test_a_check_that_fails_still_answers_the_tv(app, monkeypatch):
    Web(monkeypatch), Clock(monkeypatch)
    monkeypatch.setattr(store, "numbers_due", lambda s, now: 1 / 0)
    resp, started = ask(app)
    assert resp.status_code == 200 and not started
    assert not app.extensions["pa_numbers"]._running.locked()


def test_a_feed_that_cant_be_read_starts_nothing(app, monkeypatch):
    import lottery_tracker.pa_feeds.display as display
    web = Web(monkeypatch)
    Clock(monkeypatch)
    monkeypatch.setattr(display, "build_payload", lambda session, now=None: 1 / 0)
    resp, started = ask(app)
    assert resp.status_code == 503 and not started and web.asked == []


# --- one run at a time, the app's or GitHub's (the lock in the database) ------------------------

class Fake:
    """Stands in for the lock: ``free`` says whether it's free, and every
    connection it was taken on is kept."""

    def __init__(self, monkeypatch, free):
        self.free, self.conns = free, []
        monkeypatch.setattr(store, "try_numbers_lock", self.take)

    def take(self, conn):
        self.conns.append(conn)
        return self.free


def test_while_github_has_the_lock_the_app_leaves_the_numbers_to_it(app, url, monkeypatch,
                                                                    capsys):
    web, lock = Web(monkeypatch), Fake(monkeypatch, free=False)
    Clock(monkeypatch)
    assert ask(app)[1]
    assert web.asked == [] and setting(url, store.NUMBERS_LAST_ATTEMPT) is None
    assert in_app.BUSY in capsys.readouterr().out
    assert len(lock.conns) == 1 and lock.conns[0].closed      # let go of in the end
    lock.free = True
    assert ask(app)[1] and web.asked == [job.FEED_URL, job.OLD_SCRAPER_URL]
    assert all(c.closed for c in lock.conns)


def test_while_the_app_has_the_lock_github_skips_the_numbers_but_not_the_rest(url, monkeypatch,
                                                                              capsys):
    tg = Telegram(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", url)
    pages = {job.RETAILERS_URL: (PA / "Current-Retailers_2026-10-28.html").read_bytes(),
             "https://www.palottery.pa.gov/PaLotteryWebSite/media/Retailer-Documents/"
             "Retailer-Tools/Scratch-Offs_Order/Scratch-Offs_Order_10-26-26.pdf":
             (PA / "Scratch-Offs_Order_10-26-26.pdf").read_bytes()}
    web, lock = Web(monkeypatch, pages), Fake(monkeypatch, free=False)
    e = create_engine(url)
    store.create_tables(e)
    with Session(e) as s:
        store.quiet(s)
        store.tell(s, "an earlier mismatch", "⚠️ waiting", T0)     # left by the app
        s.commit()
    e.dispose()
    assert job.run(["all"], now=T0) == 0
    assert job.BUSY in capsys.readouterr().out
    assert job.FEED_URL not in web.asked and job.RETAILERS_URL in web.asked
    assert tg.tried[0] == "⚠️ waiting" and waiting(url) == []       # still delivered
    assert setting(url, store.NUMBERS_LAST_ATTEMPT) is None
    assert all(c.closed for c in lock.conns)


def test_the_lock_is_let_go_of_when_the_run_fails(app, url, monkeypatch):
    lock = Fake(monkeypatch, free=True)
    Web(monkeypatch), Clock(monkeypatch)
    monkeypatch.setattr(job, "numbers", lambda s, now: 1 / 0)
    assert ask(app)[1]
    assert len(lock.conns) == 1 and lock.conns[0].closed
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(ZeroDivisionError):                            # GitHub: a bug fails loudly
        job.run(["numbers"], now=T0 + timedelta(minutes=10))
    assert len(lock.conns) == 2 and lock.conns[1].closed


def test_a_github_run_counts_as_a_run(url, monkeypatch):
    Web(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", url)
    assert job.run(["numbers"], now=T0) == 0
    assert setting(url, store.NUMBERS_LAST_ATTEMPT) == setting(url, store.NUMBERS_LAST_RUN) \
        == T0.isoformat()
    app = make_app(url, PA_FEED_IN_APP=True)
    clock = Clock(monkeypatch, T0 + timedelta(minutes=4))
    assert not ask(app)[1]
    clock.at(minutes=5)
    assert ask(app)[1]


def test_a_run_that_waited_for_the_lock_skips_what_was_just_read(url, monkeypatch):
    web = Web(monkeypatch)
    app = make_app(url)
    put_setting(url, store.NUMBERS_LAST_RUN, T0.isoformat())        # it just finished
    assert in_app.run_once(app.config["SESSION_FACTORY"], T0 + timedelta(minutes=1)) == "fresh"
    assert web.asked == []
    assert in_app.run_once(app.config["SESSION_FACTORY"], T0 + timedelta(minutes=5)) == "done"


class FakePostgres:
    """Just enough of a Postgres connection to see what the lock asks of it."""

    class dialect:
        name = "postgresql"

    def __init__(self, held=False, broken=False):
        self.held, self.broken = held, broken
        self.sql, self.rolled_back, self.closed, self.invalidated = [], False, False, False

    def connect(self):                       # the engine hands out this one connection
        return self

    def execute(self, statement, params=None):
        self.sql.append((str(statement), params))

        class Result:
            def scalar(_):
                return not self.held
        return Result()

    def rollback(self):
        if self.broken:
            raise OperationalError("ROLLBACK", {}, Exception("server closed the connection"))
        self.rolled_back = True

    def close(self):
        self.closed = True

    def invalidate(self):
        self.invalidated = True


def test_on_postgres_the_lock_is_held_by_a_transaction_and_ended_with_it():
    pg = FakePostgres()
    with store.numbers_lock(pg) as mine:
        assert mine
        assert pg.sql == [("SELECT pg_try_advisory_xact_lock(:key)", {"key": store.NUMBERS_LOCK})]
        assert not pg.rolled_back                    # held while the run goes on
    assert pg.rolled_back and pg.closed               # ending the transaction lets go of it


def test_on_postgres_a_lock_held_elsewhere_means_skip_and_still_close():
    pg = FakePostgres(held=True)
    with store.numbers_lock(pg) as mine:
        assert not mine
    assert pg.rolled_back and pg.closed


def test_on_postgres_the_lock_goes_even_when_the_run_or_the_connection_fails():
    pg = FakePostgres()
    with pytest.raises(ZeroDivisionError):
        with store.numbers_lock(pg):
            1 / 0
    assert pg.rolled_back and pg.closed
    pg = FakePostgres(broken=True)
    with store.numbers_lock(pg):
        pass
    assert pg.invalidated and pg.closed               # a dropped connection frees the lock


def test_on_sqlite_there_is_no_lock_to_take(url):
    e = create_engine(url)
    with e.connect() as conn:
        assert store.try_numbers_lock(conn)
    e.dispose()


# --- quiet: the app sends nothing, and loses nothing -------------------------------------------

def test_a_mismatch_found_by_the_app_is_sent_once_by_the_next_github_run(app, url, monkeypatch):
    tg, clock = Telegram(monkeypatch), Clock(monkeypatch)
    Web(monkeypatch, {job.OLD_SCRAPER_URL: mismatched_old()})
    assert ask(app)[1]
    assert tg.tried == []                              # Telegram is set up here, and still
    assert len(waiting(url)) == 1 and "disagree" in waiting(url)[0]
    assert count(url, DrawCheckRow) == 2
    assert setting(url, store.TRIAL_STARTED) == T0.isoformat()
    monkeypatch.setenv("DATABASE_URL", url)
    for minutes in (10, 20):
        assert job.run(["numbers"], now=T0 + timedelta(minutes=minutes)) == 0
    clock.at(minutes=30)
    assert ask(app)[1]
    assert job.run(["numbers"], now=T0 + timedelta(minutes=40)) == 0
    disagree = [t for t in tg.tried if "disagree" in t]
    assert len(disagree) == 1 and "Old scraper: 08 26 49 50 54" in disagree[0]
    assert waiting(url) == []
    # The draws were compared once, and the trial started over once.
    assert count(url, DrawCheckRow) == 2
    assert setting(url, store.TRIAL_STARTED) == T0.isoformat()


def test_without_telegram_the_apps_messages_keep_waiting(app, url, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    sent = []
    monkeypatch.setattr(telegram, "send", lambda text, formatted=False: sent.append(text) or False)
    Clock(monkeypatch)
    Web(monkeypatch, {job.OLD_SCRAPER_URL: mismatched_old()})
    assert ask(app)[1]
    assert sent == [] and len(waiting(url)) == 1          # not dropped for want of Telegram
    tg = Telegram(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", url)
    assert job.run(["numbers"], now=T0 + timedelta(minutes=10)) == 0
    assert len(tg.tried) == 1 and "disagree" in tg.tried[0]


def test_a_problem_and_its_fixed_note_from_the_app_go_out_once_in_order(app, url, monkeypatch):
    tg, clock = Telegram(monkeypatch), Clock(monkeypatch)
    web = Web(monkeypatch, {job.FEED_URL: requests.ConnectTimeout("slow")})
    for minutes in range(0, 76, 5):                       # PA down for over an hour
        clock.at(minutes=minutes)
        assert ask(app)[1]
    web.pages[job.FEED_URL] = FEED
    clock.at(minutes=80)
    assert ask(app)[1]
    assert tg.tried == []
    said = waiting(url)
    assert len(said) == 2
    assert "Can't read PA's number feed" in said[0] and said[1] == store.FIXED["numbers_fetch"]
    monkeypatch.setenv("DATABASE_URL", url)
    for minutes in (90, 100):
        assert job.run(["numbers"], now=T0 + timedelta(minutes=minutes)) == 0
    assert tg.tried == said and waiting(url) == []


def test_the_switch_and_the_turn_off_reminder_from_the_app_go_out_in_that_order(url):
    # Both happen on the same run; its messages keep their order on the list.
    e = create_engine(url)
    store.create_tables(e)
    with Session(e) as s:
        store.quiet(s)
        store.tell(s, "switched", "✅ switched", T0)
        store.report(s, "old_quiet", "⚠️ quiet", T0)
        s.commit()
    e.dispose()
    assert waiting(url) == ["✅ switched", "⚠️ quiet"]


# --- turned off ----------------------------------------------------------------------------------

def test_pa_feed_in_app_0_turns_it_off(url, monkeypatch):
    web = Web(monkeypatch)
    Clock(monkeypatch)
    monkeypatch.setenv("PA_FEED_IN_APP", "0")
    app = make_app(url)
    assert app.config["PA_FEED_IN_APP"] is False
    resp, started = ask(app)
    assert resp.status_code == 200 and not started and web.asked == []
    monkeypatch.setenv("PA_FEED_IN_APP", "1")
    assert make_app(url).config["PA_FEED_IN_APP"] is True


def test_it_is_off_in_the_tests_unless_a_test_turns_it_on(url):
    assert make_app(url).config["PA_FEED_IN_APP"] is False


@pytest.mark.parametrize("value,on", [
    (None, True), ("", True), ("1", True), ("true", True), ("yes", True),
    ("0", False), ("false", False), ("OFF", False), ("no", False), ('"0"', False),
    (False, False), (True, True)])
def test_what_turns_it_off(value, on):
    assert in_app.wanted(value) is on


def test_the_daily_run_is_one_of_githubs_numbers_runs():
    import yaml
    wf = yaml.safe_load(Path(".github/workflows/pa-feeds.yml").read_text())
    numbers = (wf.get("on") or wf.get(True))["schedule"][0]["cron"]
    minutes = [int(m) for m in numbers.split()[0].split(",")]
    # Eastern time is a whole number of hours from UTC, so the minutes match.
    assert [m for m in minutes if m < job.DAILY_MINUTES] == [4]


# --- on a real Postgres (skipped unless TEST_POSTGRES_URL is set) -------------------------------
#
#   TEST_POSTGRES_URL=postgresql://postgres@127.0.0.1:5432/postgres python -m pytest -k postgres
#
# A user that may create databases: each test makes its own and drops it after.

PG = __import__("os").environ.get("TEST_POSTGRES_URL")


@pytest.fixture()
def pg_url():
    if not PG:
        pytest.skip("TEST_POSTGRES_URL isn't set")
    import uuid
    from sqlalchemy import text
    name = f"pa_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(PG, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    yield PG.rsplit("/", 1)[0] + "/" + name
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def _advisory_locks(url):
    from sqlalchemy import text
    e = create_engine(url)
    with e.connect() as c:
        n = c.execute(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                           "AND database = (SELECT oid FROM pg_database "
                           "WHERE datname = current_database())")).scalar()
    e.dispose()
    return n


def test_on_postgres_one_run_at_a_time_and_nothing_left_held(pg_url, monkeypatch):
    slow = Slow(FEED)
    web = Web(monkeypatch, {job.FEED_URL: slow})
    app = make_app(pg_url, PA_FEED_IN_APP=True)
    engine = app.config["SESSION_FACTORY"].kw["bind"]
    gate, answers = threading.Barrier(6), []

    def tv():
        gate.wait(5)
        answers.append(app.test_client().get("/api/numbers").status_code)
    tvs = [threading.Thread(target=tv) for _ in range(6)]
    for t in tvs:
        t.start()
    for t in tvs:
        t.join(10)
    run = app.extensions["pa_numbers"].thread
    try:
        assert answers == [200] * 6 and run.is_alive()
        assert _advisory_locks(pg_url) == 1                 # held while it waits on PA
        monkeypatch.setenv("DATABASE_URL", pg_url)
        assert job.run(["numbers"]) == 0                    # GitHub meanwhile: leaves it
        assert web.asked.count(job.FEED_URL) == 1
    finally:
        slow.go.set()
        run.join(10)
    assert not run.is_alive()
    assert _advisory_locks(pg_url) == 0 and engine.pool.checkedout() == 0
    assert len(app.test_client().get("/api/numbers").get_json()["games"]) == 16
    # A run that fails lets go of the lock and its connections too.
    monkeypatch.setattr(job, "numbers", lambda s, now: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        in_app.run_once(app.config["SESSION_FACTORY"], store.utcnow() + timedelta(hours=1))
    assert _advisory_locks(pg_url) == 0 and engine.pool.checkedout() == 0
    engine.dispose()


def test_on_postgres_a_run_skips_while_another_holds_the_lock(pg_url):
    from sqlalchemy import text
    app = make_app(pg_url)
    other = create_engine(pg_url).connect()                 # GitHub's run, say
    other.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": store.NUMBERS_LOCK})
    try:
        assert in_app.run_once(app.config["SESSION_FACTORY"]) == "busy"
    finally:
        other.rollback()                                    # its run ends
        other.close()
    engine = app.config["SESSION_FACTORY"].kw["bind"]
    with store.numbers_lock(engine) as mine:
        assert mine
    assert _advisory_locks(pg_url) == 0
    engine.dispose()
