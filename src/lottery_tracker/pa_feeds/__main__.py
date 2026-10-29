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
problem doesn't turn into a failure email every ten minutes. Only a real crash,
a bug in this code, ends with an error.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
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
        body = download(FEED_URL)
    except FetchError as e:
        store.fetch_failed(session, "numbers", "PA's number feed", str(e), now,
                           meanwhile="The TVs keep showing the last numbers read.")
    else:
        store.fetch_worked(session, "numbers", now)
        try:
            reading = draws_.read_feed(body)
        except draws_.FeedError as e:
            store.report(session, "numbers_format", store.problem_list(
                "PA's number feed changed, so nothing from it was saved. The TVs keep "
                "the last numbers read.", [str(e)]), now)
        else:
            changed = store.save_draws(session, reading.draws, now)
            store.remember_feed_order(session, reading.order, now)
            store.report(session, "numbers_format", store.problem_list(
                "PA's number feed needs a look", reading.problems + changed), now)
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
            store.report(session, "old_format", store.problem_list(
                "The old Powerball/Mega Millions scraper's file needs a look",
                reading.problems + changed), now)
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
    try:
        page = download(RETAILERS_URL)
    except FetchError as e:
        store.fetch_failed(session, "orders_page", "PA's retailer page", str(e), now,
                           meanwhile="The last order list read is still in use.")
        return
    store.fetch_worked(session, "orders_page", now)

    try:
        url = orders_.find_order_link(page, RETAILERS_URL)
    except orders_.OrderListError as e:
        store.report(session, "orders_format", store.problem_list(
            "PA's order list couldn't be found", [str(e)]), now)
        return
    if _already_have(session, url):
        store.report(session, "orders_format", None, now)
        print(f"Order list already saved: {url}", flush=True)
        return

    try:
        pdf = download(url)
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
            store.send(message)
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

    url = _env("DATABASE_URL")
    if not url:
        print(NO_DATABASE)
        return 0
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(_db_url(url), future=True)
    try:
        store.create_tables(engine)
        now = now or store.utcnow()
        with Session(engine) as session:
            if args.what in ("numbers", "all"):
                numbers(session, now)
                session.commit()
            if args.what in ("orders", "all"):
                orders(session, now)
                session.commit()
    finally:
        engine.dispose()
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
