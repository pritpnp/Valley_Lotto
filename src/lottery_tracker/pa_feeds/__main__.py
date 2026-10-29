"""Read PA's feeds and save them. Run on a schedule by .github/workflows/pa-feeds.yml.

    python -m lottery_tracker.pa_feeds numbers     # winning numbers (every 10 minutes)
    python -m lottery_tracker.pa_feeds orders      # the weekly order list (hourly)
    python -m lottery_tracker.pa_feeds all         # both
    python -m lottery_tracker.pa_feeds all --dry-run   # read and print only:
                                                       # no database, no Telegram

Settings, kept as GitHub secrets and never in the code: DATABASE_URL (the same
Supabase link the app uses), TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.

Anything PA or the network can cause (a page that doesn't answer, a format
change) is told to Telegram once and the run still ends normally (exit 0), so a
problem doesn't turn into a failure email every ten minutes. A database that
can't be reached (or stops answering part way through), turns down the login,
won't save anything (read-only), or a DATABASE_URL that isn't a link this can
use, is a warning in the log, for the same reason. The database can't remember
that the owner was told about a problem with the database itself, so a lasting
one is told on one run a day instead: the one that starts between 9:00 and 9:10
in the morning, Eastern time. (The keepalive check can't see any of this: it
checks the app, which has its own copy of the link on Render.) Only a real
crash, a bug in this code, ends with an error.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from ..web.models import OrderListRow
from . import draws as draws_
from . import orders as orders_
from . import store

FEED_URL = "https://www.palottery.pa.gov/game-number-feed.aspx"
OLD_SCRAPER_URL = ("https://raw.githubusercontent.com/pritpnp/powerball-megamillion/"
                   "main/lottery.json")
RETAILERS_URL = ("https://www.palottery.pa.gov/About-PA-Lottery/For-Retailers/"
                 "Current-Retailers.aspx")
STATE_PATH = Path(__file__).resolve().parents[3] / "data" / "state.json"

NO_DATABASE = ("Nothing saved: the database link has to be added as a GitHub secret "
               "named DATABASE_URL (the same Supabase link the app uses) before PA's "
               "numbers and order list can be stored.")
BAD_DATABASE = ("Nothing saved: the GitHub secret DATABASE_URL isn't a database link this "
                "can use. Copy the link the app uses from Supabase again and save it as "
                "that secret.")

# A problem with the database itself, told to Telegram on the daily run (see
# daily_run). Never with the link or any part of it: it holds the password.
SECRET_STEPS = ("To fix it, copy the link the app uses: in Render, open the app → "
                "Environment → DATABASE_URL. Then in GitHub open Valley_Lotto → Settings "
                "→ Secrets and variables → Actions, and update the secret DATABASE_URL "
                "with it.")
NOT_SAVED = "⚠️ <b>PA's numbers aren't being saved</b>"
MEANWHILE = ("Until then the TVs keep showing the last numbers saved. This message "
             "comes once a day while it lasts.")
LOGIN_TURNED_DOWN = "\n".join([
    NOT_SAVED,
    "The database turned down the feed job's login: the link in the GitHub secret "
    "DATABASE_URL has an old or wrong password or user. That happens when the "
    "Supabase database password is changed and only Render is given the new link.",
    SECRET_STEPS, MEANWHILE])
NOT_REACHED = "\n".join([
    NOT_SAVED,
    "The feed job can't reach the database. If Supabase is down or paused, this "
    "clears up by itself. If the database password was changed, the GitHub secret "
    "DATABASE_URL still has the old one.",
    SECRET_STEPS, MEANWHILE])
NOT_A_LINK = "\n".join([
    NOT_SAVED,
    "The GitHub secret DATABASE_URL isn't a database link the feed job can use.",
    SECRET_STEPS, MEANWHILE])
READ_ONLY = "\n".join([
    NOT_SAVED,
    "The database won't save anything new: Supabase has made it read-only. It does "
    "that when a project goes over its plan's size limit, and then the store app "
    "can't save either.",
    "To fix it, open the project on supabase.com, then free up space or upgrade "
    "the plan.",
    MEANWHILE])

# The daily run: the one that starts in these ten minutes, Eastern time. The
# numbers run every ten minutes, so about one run a day falls in them.
DAILY_HOUR, DAILY_MINUTES = 9, 10

# PA's site turns away anything that doesn't look like a browser.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 30          # seconds per try
RETRY_WAIT = 5        # seconds before the one retry


class FetchError(RuntimeError):
    pass


def _why(e: Exception) -> str:
    """A short reason, never a whole URL or page."""
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"the site answered {e.response.status_code}"
    if isinstance(e, FetchError):
        return str(e)
    return type(e).__name__


def download(url: str) -> bytes:
    """GET ``url`` as a browser would, with a timeout and one retry."""
    last: Exception | None = None
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            if not resp.content or not resp.content.strip():
                raise FetchError("the answer was empty")
            return resp.content
        except Exception as e:  # noqa: BLE001 — reported as a FetchError below
            last = e
            if attempt == 0:
                time.sleep(RETRY_WAIT)
    raise FetchError(_why(last))


def _env(name: str) -> str | None:
    """An env var, tolerating surrounding quotes (as the app does)."""
    val = (os.environ.get(name) or "").strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1].strip()
    return val or None


def _eastern(now: datetime) -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 — no time zone data: Eastern standard time
        return now.astimezone(timezone(timedelta(hours=-5)))


def daily_run(now: datetime) -> bool:
    """The run that starts between 9:00 and 9:10 in the morning, Eastern time.

    A problem with the database itself (it can't be reached, turns down the
    login, or won't save anything) can't be remembered in that database, and
    neither can having told the owner about it. So it's told on this run only,
    which needs nothing saved: once a day while it lasts, never every ten
    minutes."""
    local = _eastern(now)
    return local.hour == DAILY_HOUR and local.minute < DAILY_MINUTES


def _refuses_writes(e: Exception) -> bool:
    """The database is read-only: Supabase's read-only mode (Postgres code
    25006), or a read-only SQLite file."""
    orig = getattr(e, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code == "25006" or str(getattr(orig, "sqlite_errorname", "")).startswith(
        "SQLITE_READONLY")


# How Postgres and Supabase's pooler say the login itself was wrong, rather than
# the database being down. Only used to pick the words: the message itself
# names the server and the user, and is never shown.
_LOGIN_WORDS = ("password authentication failed", "tenant or user not found",
                "no pg_hba.conf entry", "does not exist")


def _login_turned_down(e: Exception) -> bool:
    orig = getattr(e, "orig", None) or e
    code = str(getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None) or "")
    return code.startswith("28") or any(w in str(orig).lower() for w in _LOGIN_WORDS)


def _db_url(url: str) -> str:
    """Supabase's ``postgres://`` link -> SQLAlchemy's psycopg driver (as the app does)."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


# --------------------------------------------------------------------------- #
# numbers: PA's feed + the old scraper, saved and compared
# --------------------------------------------------------------------------- #

def numbers(session, now: datetime) -> None:
    switched = store.pbmm_source(session) == draws_.PA_FEED
    pa_ok = False      # PA's feed gave both Powerball and Mega Millions this time

    try:
        reading = draws_.read_feed(download(FEED_URL))
    except (FetchError, draws_.FeedError) as e:
        # Something that isn't the feed at all (a busy page, a robot check) is
        # treated like no answer: PA's site gives one now and then, and each
        # flip between it and the feed isn't news. Nothing from it is saved, and
        # if it keeps up for over an hour the owner is told once, with the reason.
        store.fetch_failed(session, "numbers", "PA's number feed", str(e), now,
                           meanwhile="The TVs keep showing the last numbers read.")
    else:
        store.fetch_worked(session, "numbers", now)
        changed = store.save_draws(session, reading.draws, now)
        store.remember_feed_order(session, reading.order, now)
        problems = reading.problems + changed
        store.report(session, "numbers_format", store.problem_list(
            "PA's number feed needs a look", problems), now,
            key=store.problem_key(problems))
        print(f"PA's number feed: {len(reading.draws)} games read.", flush=True)
        pa_ok = set(store.PBMM) <= {d.game_id for d in reading.draws}
    store.note_pa_feed(session, pa_ok, now)

    # The old scraper. Once the TVs have switched to PA's feed it's expected to
    # stop, so from then on its silence isn't a problem. It's still read: whether
    # it's running decides the "turn it OFF" reminder, and if the owner turns it
    # back on while PA's feed is stale, the TVs show its newer draws.
    try:
        body = download(OLD_SCRAPER_URL)
    except FetchError as e:
        if switched:
            print(f"Old scraper's file not read ({e}); the TVs use PA's feed now.", flush=True)
        else:
            store.fetch_failed(session, "old", "the old Powerball/Mega Millions scraper's "
                               "file", str(e), now,
                               meanwhile="The side-by-side check with PA's feed waits for it.")
    else:
        store.fetch_worked(session, "old", now)
        try:
            reading = draws_.read_old_scraper(body)
        except draws_.FeedError as e:
            reading = draws_.Reading([], [str(e)], complete=False)
        changed = store.save_draws(session, reading.draws, now)
        if not switched:
            problems = reading.problems + changed
            store.report(session, "old_format", store.problem_list(
                "The old Powerball/Mega Millions scraper's file needs a look", problems),
                now, key=store.problem_key(problems))
        print(f"Old scraper: {len(reading.draws)} games read.", flush=True)

    result = store.compare_and_switch(session, now)
    store.check_old_scraper(session, now)
    store.advise_old_scraper(session, now)
    print(f"Compared {result['checked']} new draw(s) with the old scraper; "
          f"TVs use {store.pbmm_source(session)} for Powerball and Mega Millions.", flush=True)


# --------------------------------------------------------------------------- #
# orders: the weekly "Scratch-Offs Available for Ordering" PDF
# --------------------------------------------------------------------------- #

def _known_games() -> dict:
    """The tracker's games (``data/state.json``), for the cross-check."""
    try:
        from ..state import load_state
        return load_state(STATE_PATH)
    except Exception as e:  # noqa: BLE001 — the cross-check is a nicety
        print(f"Couldn't read {STATE_PATH.name} for the cross-check ({type(e).__name__}).")
        return {}


def _already_have(session, url: str) -> bool:
    """This week's link was read already (same link, or a list with its date)."""
    if store.saved_order_list(session, url) is not None:
        return True
    if store.get_setting(session, store.ORDERS_LAST_URL) == url:
        return True
    linked = orders_.url_date(url)
    return linked is not None and session.get(OrderListRow, linked) is not None


def orders(session, now: datetime) -> None:
    # A page without the link may be a busy page or a robot check rather than a
    # redesign, so it's treated like a page that didn't answer: only news once
    # it has kept up for over an hour.
    try:
        url = orders_.find_order_link(download(RETAILERS_URL), RETAILERS_URL)
    except (FetchError, orders_.OrderListError) as e:
        store.fetch_failed(session, "orders_page", "PA's retailer page", str(e), now,
                           meanwhile="The last order list read is still in use.")
        return
    store.fetch_worked(session, "orders_page", now)
    if _already_have(session, url):
        store.report(session, "orders_format", None, now)
        print(f"Order list already saved: {url}", flush=True)
        return

    try:
        pdf = download(url)
        if b"%PDF-" not in pdf[:1024]:
            # A web page in the PDF's place: like no answer, not a changed list.
            raise FetchError("the answer wasn't a PDF")
    except FetchError as e:
        store.fetch_failed(session, "orders_pdf", "PA's order list", str(e), now,
                           meanwhile="The last order list read is still in use.")
        return
    store.fetch_worked(session, "orders_pdf", now)

    try:
        order_list = orders_.parse_order_pdf(pdf)
    except orders_.OrderListError as e:
        store.report(session, "orders_format", store.problem_list(
            "PA's order list changed, so it wasn't used. The last one read is still "
            "in use.", [str(e), url]), now)
        return
    store.report(session, "orders_format", None, now)

    result = store.save_order_list(session, order_list, url, pdf, now)
    store.set_setting(session, store.ORDERS_LAST_URL, url, now)
    if result == "different":
        when = store.nice_date(order_list.as_of)
        store.report(session, "orders_conflict", store.problem_list(
            f"PA posted a different order list that's also dated as of {when}. Nothing "
            f"was changed: the one saved first is kept.", [url]),
            now, key=f"{order_list.as_of} {store.pdf_hash(pdf)}")
        return
    store.report(session, "orders_conflict", None, now)
    print(f"Order list as of {order_list.as_of}: {len(order_list.games)} games ({result}).",
          flush=True)
    if result == "saved":
        message = store.order_change_message(session, order_list)
        if message:
            store.tell(session, f"orders {order_list.as_of}", message, now)
    store.report(session, "orders_check", store.problem_list(
        "PA's order list: worth a look", orders_.cross_check(order_list, url, _known_games())),
        now)


# --------------------------------------------------------------------------- #
# --dry-run: read and print, nothing saved or sent
# --------------------------------------------------------------------------- #

def _show(d: draws_.Draw) -> str:
    extras = "".join(f" · {e['label']} {e['value']}" for e in d.extras)
    jackpot = f"  jackpot {d.jackpot_text}" if d.jackpot_text else ""
    cash = f" (cash {d.cash_value_text})" if d.cash_value_text else ""
    nxt = f"  next {d.jackpot_date}" if d.jackpot_date else ""
    return f"  {d.game_id:>3} {d.title}: {d.draw_date}  {' '.join(d.numbers)}{extras}{nxt}{jackpot}{cash}"


def dry_run(what: str) -> int:
    if what in ("numbers", "all"):
        for label, url, read in (("PA's number feed", FEED_URL, draws_.read_feed),
                                 ("Old scraper", OLD_SCRAPER_URL, draws_.read_old_scraper)):
            try:
                reading = read(download(url))
            except (FetchError, draws_.FeedError) as e:
                print(f"{label}: {e}")
                continue
            print(f"{label}: {len(reading.draws)} games")
            for d in reading.draws:
                print(_show(d))
            for p in reading.problems:
                print(f"  ! {p}")
    if what in ("orders", "all"):
        try:
            url = orders_.find_order_link(download(RETAILERS_URL), RETAILERS_URL)
            order_list = orders_.parse_order_pdf(download(url))
        except (FetchError, orders_.OrderListError) as e:
            print(f"Order list: {e}")
            return 0
        print(f"Order list {url}\n  as of {order_list.as_of}: {len(order_list.games)} games")
        for g in order_list.games:
            print(f"  {g.game_number} ${g.price:g} {g.tickets_per_pack}/pack {g.name}")
        for note in orders_.cross_check(order_list, url, _known_games()):
            print(f"  ! {note}")
    return 0


# --------------------------------------------------------------------------- #

def run(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m lottery_tracker.pa_feeds",
                                 description="Read PA's winning numbers and order list.")
    ap.add_argument("what", choices=["numbers", "orders", "all"])
    ap.add_argument("--dry-run", action="store_true",
                    help="read and print only: no database, no Telegram")
    args = ap.parse_args(argv)
    if args.dry_run:
        return dry_run(args.what)
    now = now or store.utcnow()

    def tell_daily(text: str) -> None:
        # Only the numbers job (every ten minutes) tells: the hourly order-list
        # run can start in the same ten minutes.
        if args.what != "orders" and daily_run(now):
            store.send(text)

    url = _env("DATABASE_URL")
    if not url:
        print(NO_DATABASE)
        return 0
    from sqlalchemy import create_engine
    from sqlalchemy.exc import ArgumentError, DBAPIError, NoSuchModuleError, OperationalError
    from sqlalchemy.orm import Session

    # Only the kind of error is ever shown below: the link holds the password,
    # the full messages name the database's server and user, and this log is
    # public.
    try:
        engine = create_engine(_db_url(url), future=True)
    except (ArgumentError, NoSuchModuleError, ValueError) as e:
        # A mistyped secret, not a bug: a failed run every ten minutes would be
        # an email each time until it's fixed.
        print(f"::warning::{BAD_DATABASE} ({type(e).__name__})", flush=True)
        tell_daily(NOT_A_LINK)
        return 0
    try:
        try:
            with engine.connect():
                pass
        except OperationalError as e:
            # Not a bug, so not a failed run (every ten minutes, that would be
            # dozens of emails). Nothing else would ever tell the owner, though:
            # the TVs would just show older and older numbers.
            kind = type(e.orig or e).__name__
            if _login_turned_down(e):
                # A wrong or old password in the GitHub secret (Supabase's was
                # reset and only Render was given the new one), or a wrong user.
                print(f"::warning::The database turned down the login ({kind}), so "
                      f"nothing was read or saved this time. The GitHub secret "
                      f"DATABASE_URL needs the same link the app uses on Render.", flush=True)
                tell_daily(LOGIN_TURNED_DOWN)
            else:
                # Supabase down or paused, or a network blip.
                print(f"::warning::The database couldn't be reached ({kind}), so nothing "
                      f"was read or saved this time. The next run tries again.", flush=True)
                tell_daily(NOT_REACHED)
            return 0
        except DBAPIError as e:
            # It answered, but the link has something it won't take (an option
            # like ?pgbouncer=true): the secret again, not a bug.
            print(f"::warning::{BAD_DATABASE} ({type(e.orig or e).__name__})", flush=True)
            tell_daily(NOT_A_LINK)
            return 0
        try:
            store.create_tables(engine)
            with Session(engine) as session:
                store.deliver_pending(session, now)     # what earlier runs couldn't get through
                if args.what in ("numbers", "all"):
                    numbers(session, now)
                    session.commit()
                if args.what in ("orders", "all"):
                    orders(session, now)
                    session.commit()
        except DBAPIError as e:
            kind = type(e.orig or e).__name__
            if _refuses_writes(e):
                # Supabase's read-only mode (a project over its size limit). A
                # message is only sent once that it was sent is saved, so none
                # went out: the owner hears about this instead, once a day.
                print(f"::warning::The database won't save anything: it's read-only "
                      f"({kind}). Nothing was saved this time. The next run tries again.",
                      flush=True)
                tell_daily(READ_ONLY)
                return 0
            if isinstance(e, OperationalError):
                # The database went away part way through: the same blip as above.
                # What wasn't saved yet is read and saved again next time.
                print(f"::warning::The database stopped answering part way through "
                      f"({kind}). The next run tries again.", flush=True)
                return 0
            raise
    finally:
        engine.dispose()
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
