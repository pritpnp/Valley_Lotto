"""Saving what the feeds read, and deciding when PA's feed takes over.

Everything here works on a SQLAlchemy session over the app's own database
(Supabase in production, SQLite in tests). The tables are defined in
``lottery_tracker.web.models``; this module only creates the ``pa_`` ones.

Three jobs live here:

* **Saving.** Each draw is saved once per source; later readings only refresh
  when it was last seen and the jackpot (estimates move). An order list is saved
  once per "As of" date.
* **Telling the owner, once.** A problem goes to Telegram the first time it's
  seen, not every ten minutes; a short "fixed" note follows when it clears. A
  website that doesn't answer, or answers with something that isn't the page at
  all, is only mentioned once it has kept failing for over an hour. That a
  message was sent is saved before it goes, so a run that fails afterwards (or
  a database that won't save anything) can't have it sent again every ten
  minutes; one Telegram doesn't take is tried again at the next run.
* **The side-by-side check.** Powerball and Mega Millions are read from PA's
  feed and from the old scraper; every draw is compared. After 14 days of exact
  matches (at least 4 draws of each game) the TVs switch to PA's feed, for good.
* **The old scraper's on/off switch.** Its workflow lives in another repo, so the
  owner turns it on or off by hand. After the switch he's told to turn it OFF
  while it still looks like it's running, and to turn it back ON if PA's feed
  stops being trustworthy for Powerball or Mega Millions.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .. import telegram
from ..web import migrate
from ..web.models import (Base, DrawCheckRow, DrawResultRow, FeedSettingRow,
                          OrderGameRow, OrderListRow)
from .draws import EXTRA_NAMES, MEGA_MILLIONS, OLD_SCRAPER, PA_FEED, POWERBALL, Draw
from .orders import OrderList

PA_TABLES = [DrawResultRow.__table__, DrawCheckRow.__table__, FeedSettingRow.__table__,
             OrderListRow.__table__, OrderGameRow.__table__]

# Settings kept in pa_feed_settings.
PBMM_SOURCE = "pbmm_source"            # "old_scraper" until the switch, then "pa_feed"
TRIAL_STARTED = "trial_started_at"     # first comparison; reset by any mismatch
SWITCHED_AT = "switched_at"
FEED_ORDER = "feed_order"              # PA's game ids, in the feed's order (JSON)
ORDERS_LAST_URL = "orders_last_url"    # the last order-list link read through
OLD_LAST_NEW = "old_last_new_at"       # the last time the old scraper showed a newer draw
OLD_OFF_TOLD = "old_off_told_at"       # the last "turn the old scraper OFF" message
OLD_ON_TOLD = "old_on_told"            # why "turn it back ON" was last sent (JSON list)
PA_FAILING_SINCE = "pa_failing_since"  # PA's feed gave no Powerball/Mega Millions since
PENDING = "pending:"                   # + what happened: a message still to be delivered

# The games both sources cover: id -> (name, bonus ball code).
PBMM = {POWERBALL: ("Powerball", "PB"), MEGA_MILLIONS: ("Mega Millions", "MB")}

TRIAL_DAYS = 14
TRIAL_DRAWS = 4                        # matched draws of EACH game during the trial
OLD_SCRAPER_QUIET = timedelta(days=3)
FETCH_GRACE = timedelta(hours=1)
OLD_RUNNING = timedelta(days=4)        # a newer draw within this: the old scraper is running
OFF_REMINDER = timedelta(days=7)       # "turn it OFF" at most this often
PA_STALE_DAYS = 4                      # PA's newest Powerball/Mega Millions older than this
DRAW_NIGHT = timedelta(hours=6)        # a night's draws are posted after midnight: for
                                       # "days old", a day starts at 6am Eastern
PA_FAILING = timedelta(hours=6)        # PA's feed failing for longer than this

# Where the owner turns the old scraper's daily run on or off.
OLD_SCRAPER_STEPS = ('GitHub → powerball-megamillion → Actions → "Daily Lottery Update" '
                     '→ ⋯ (top right) → {action} workflow')

# What the owner is told when a problem clears. None: it clears quietly.
FIXED = {
    "numbers_fetch": "✅ PA's number feed can be read again.",
    "numbers_format": "✅ PA's number feed reads normally again.",
    "old_fetch": "✅ The old Powerball/Mega Millions scraper's file can be read again.",
    "old_format": "✅ The old Powerball/Mega Millions scraper's file reads normally again.",
    "old_quiet": "✅ The old scraper is sending Powerball/Mega Millions draws again.",
    "orders_page_fetch": "✅ PA's retailer page can be read again.",
    "orders_pdf_fetch": "✅ PA's order list downloads again.",
    "orders_format": "✅ PA's order list reads normally again.",
    "orders_check": None,
    "orders_conflict": None,
    "old_on": ("✅ PA's feed is fine again, you can turn the old scraper OFF again: "
               + OLD_SCRAPER_STEPS.format(action="Disable") + "."),
}


def create_tables(engine) -> None:
    """Create the five ``pa_`` tables if missing (nothing else), and turn on
    row-level security so Supabase's public API can't reach them. Only these
    five: the store app's own tables (scans, users, ...) are the app's to
    protect, on its boot, and this runs every ten minutes. Once a table is
    protected, this takes no lock on it at all."""
    Base.metadata.create_all(engine, tables=PA_TABLES)
    migrate.enable_rls(engine, tables=[t.name for t in PA_TABLES])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite hands datetimes back without a timezone; they were saved as UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _from_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return aware(datetime.fromisoformat(text))
    except ValueError:
        return None


def nice_date(iso: str | None) -> str:
    """``"2026-10-26"`` -> ``"Mon Oct 26, 2026"``."""
    if not iso:
        return "?"
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d:%a %b} {d.day}, {d.year}"


def nice_time(dt: datetime) -> str:
    """A moment as the store reads a clock: ``"Oct 28, 3:10 PM"`` Eastern."""
    try:
        from zoneinfo import ZoneInfo
        dt = aware(dt).astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 — no time zone data: say UTC instead
        return f"{aware(dt):%b} {dt.day}, {dt:%H:%M} UTC"
    hour = dt.hour % 12 or 12
    return f"{dt:%b} {dt.day}, {hour}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def get_setting(session, key: str, default: str | None = None) -> str | None:
    row = session.get(FeedSettingRow, key)
    return row.value if row is not None and row.value != "" else default


def set_setting(session, key: str, value: str, now: datetime) -> None:
    row = session.get(FeedSettingRow, key)
    if row is None:
        session.add(FeedSettingRow(key=key, value=value, updated_at=now))
    elif row.value != value:
        row.value, row.updated_at = value, now
    session.flush()


def pbmm_source(session) -> str:
    return get_setting(session, PBMM_SOURCE, OLD_SCRAPER)


# --------------------------------------------------------------------------- #
# Telling the owner, once
# --------------------------------------------------------------------------- #

def esc(text: str) -> str:
    """PA's text made safe for Telegram's HTML (only &, < and > matter there)."""
    return html.escape(text, quote=False)


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def send(text: str) -> bool:
    """One Telegram message (HTML; any PA text in it already escaped)."""
    print(_plain(text), flush=True)
    return telegram.send(text, formatted=True)


def _send_and_remember(session, key: str, value: str, text: str, now: datetime) -> bool:
    """Send ``text``, remembering that it went by setting ``key`` to ``value``.

    That's saved for good (with all the run has done so far) before the message
    goes. So a database that won't save it (Supabase makes one read-only when
    it's over its size limit) stops the message too, and nothing that goes wrong
    later in the run can undo it and have the same message sent again at every
    run. If Telegram doesn't take the message, ``key`` is put back as it was, so
    the next run tries again. (With no Telegram set up, it stays: there's nothing
    to try again.) Returns True if the message was delivered."""
    before = get_setting(session, key, "")
    set_setting(session, key, value, now)
    session.commit()
    if send(text):
        return True
    if telegram.configured():
        set_setting(session, key, before, now)
        session.commit()
    return False


def report(session, slot: str, problem: str | None, now: datetime, *,
           key: str | None = None) -> bool:
    """Tell Telegram about ``problem`` (HTML) unless it was the last thing said
    about ``slot``. ``problem=None`` means the slot is fine now: a short "fixed"
    note goes out if a problem had been reported. ``key`` is what makes two
    problems "the same" (the text itself when not given). Returns True if a
    message went out."""
    setting = f"problem:{slot}"
    before = get_setting(session, setting, "")
    if problem:
        signature = hashlib.sha256((key or problem).encode()).hexdigest()
        if signature == before:
            print(_plain(problem) + " (already reported)", flush=True)
            return False
        # Remembered once it's delivered (or there's no Telegram to deliver it
        # to), so a message lost to a Telegram outage is tried again next run.
        _send_and_remember(session, setting, signature, problem, now)
        return True
    if before:
        set_setting(session, setting, "", now)
        session.commit()            # saved before the note goes, as above
        if FIXED.get(slot):
            send(FIXED[slot])
            return True
    return False


def tell(session, event: str, text: str, now: datetime) -> None:
    """Tell the owner about something that happened once (a mismatch, the
    switch, a new order list), and make sure it gets there. The message waits in
    pa_feed_settings until Telegram takes it, so one lost to a Telegram outage
    goes out at a later run instead of never. Only the message is tried again:
    what it's about was dealt with, and saved, when it happened. ``event`` names
    it (one message per event)."""
    set_setting(session, PENDING + event, text, now)
    deliver_pending(session, now)


def deliver_pending(session, now: datetime) -> int:
    """Send the messages still waiting (see :func:`tell`), oldest first. Each is
    taken off the list for good before it goes (as in :func:`_send_and_remember`);
    one Telegram doesn't take goes back on, and the rest wait for the next run,
    since Telegram is likely down. Returns how many went out."""
    waiting = session.scalars(select(FeedSettingRow)
                              .where(FeedSettingRow.key.startswith(PENDING))
                              .order_by(FeedSettingRow.updated_at, FeedSettingRow.key)).all()
    delivered = 0
    for row in waiting:
        key, text, queued = row.key, row.value, row.updated_at
        session.delete(row)
        session.commit()
        if not text:
            continue
        if send(text) or not telegram.configured():
            delivered += 1
            continue
        session.add(FeedSettingRow(key=key, value=text, updated_at=queued))
        session.commit()
        break
    return delivered


_QUOTED = re.compile(r"“[^”]*”")


def problem_key(problems: list[str]) -> str:
    """What makes a list of format problems "the same" from one run to the next:
    the lines with the values they quote (“…”) taken out. Those are the numbers,
    dates and amounts PA or the old scraper wrote, which change at every draw;
    without this, a format change that lasts would be news again at each new
    draw. (The message itself still quotes them.)"""
    return "\n".join(_QUOTED.sub("“…”", p) for p in problems)


def problem_list(title: str, problems: list[str]) -> str | None:
    """``⚠️ title`` and one line per problem (plain text, escaped here)."""
    if not problems:
        return None
    return "\n".join([f"⚠️ <b>{esc(title)}</b>"]
                     + [f"• {esc(p)}" for p in problems])


def fetch_failed(session, name: str, what: str, error: str, now: datetime, *,
                 meanwhile: str = "") -> bool:
    """A download failed, or what came back wasn't the page at all. Remember
    when the failures started; only tell the owner once they've gone on for
    over an hour (a blip isn't news). ``meanwhile`` says what happens in the
    meantime, in plain words."""
    since = _from_iso(get_setting(session, f"failing_since:{name}"))
    if since is None:
        set_setting(session, f"failing_since:{name}", now.isoformat(), now)
        print(f"{what}: {error} (first failure; will mention it after an hour)", flush=True)
        return False
    if now - since <= FETCH_GRACE:
        print(f"{what}: {error} (failing since {nice_time(since)})", flush=True)
        return False
    text = (f"⚠️ Can't read {esc(what)}: it has kept failing since "
            f"{nice_time(since)}. {esc(meanwhile)} "
            f"<i>({esc(error)})</i>").replace("  ", " ")
    return report(session, f"{name}_fetch", text, now, key=since.isoformat())


def fetch_worked(session, name: str, now: datetime) -> None:
    set_setting(session, f"failing_since:{name}", "", now)
    report(session, f"{name}_fetch", None, now)


# --------------------------------------------------------------------------- #
# Draws
# --------------------------------------------------------------------------- #

def _bare(extras) -> list[tuple]:
    return [(e.get("code"), e.get("value")) for e in extras]


def _too_long(column: str, value: str | None) -> bool:
    """Longer than its column holds. Postgres refuses such text, and the whole
    run would be lost with it (SQLite, in the tests, doesn't check)."""
    limit = DrawResultRow.__table__.c[column].type.length
    return value is not None and limit is not None and len(value) > limit


def _unholdable(value: str | None) -> bool:
    """Holds the NUL character, which Postgres can't store in any text column
    (SQLite, in the tests, can). Like text that's too long, it would lose the
    whole run, every run. Only the old scraper's JSON can carry one."""
    return value is not None and "\x00" in value


def _fit(d: Draw) -> tuple[Draw | None, list[str]]:
    """The draw as it can be saved, and a note for each thing left out because
    the database can't hold it. None when that's the game itself (its id, name
    or numbers); a jackpot or cash value is left out on its own."""
    numbers = " ".join(d.numbers)
    name = d.title.replace("\x00", "")           # what the notes quote, never the NUL
    if (_too_long("game_id", d.game_id) or _too_long("title", d.title)
            or _too_long("numbers", numbers)):
        name = name if len(name) <= 60 else name[:60] + "…"
        return None, [f"{name}: its id, name or numbers are longer than the database "
                      f"can hold; left out."]
    if any(_unholdable(v) for v in (d.game_id, d.title, numbers)):
        return None, [f"{name}: its id, name or numbers have a character the database "
                      f"can't hold; left out."]
    notes = []
    for text, amount, label in (("jackpot_text", "jackpot_amount", "jackpot"),
                                ("cash_value_text", "cash_value_amount", "cash value")):
        value = getattr(d, text)
        if _too_long(text, value):
            notes.append(f"{name}: the {label} “{value[:40].replace(chr(0), '')}…” is longer "
                         f"than the database can hold; left out.")
            d = replace(d, **{text: None, amount: None})
        elif _unholdable(value):
            notes.append(f"{name}: the {label} has a character the database can't "
                         f"hold; left out.")
            d = replace(d, **{text: None, amount: None})
    return d, notes


def save_draws(session, draws: list[Draw], now: datetime) -> list[str]:
    """Save each draw once per source; later readings of the same draw refresh
    ``last_seen_at`` and the jackpot fields. Returns a note for each draw whose
    numbers now read differently from what was first saved (the first reading is
    kept, and the owner is told, since which one is right can't be guessed), and
    for anything left out because it's too long to save."""
    notes = []
    old_news = False
    for d in draws:
        d, too_long = _fit(d)
        notes += too_long
        if d is None:
            continue
        row = session.scalar(select(DrawResultRow).filter_by(
            source=d.source, game_id=d.game_id, draw_date=d.draw_date))
        numbers = " ".join(d.numbers)
        if row is None:
            if d.source == OLD_SCRAPER:
                newest = session.scalar(select(func.max(DrawResultRow.draw_date))
                                        .filter_by(source=OLD_SCRAPER, game_id=d.game_id))
                old_news = old_news or newest is None or d.draw_date > newest
            session.add(DrawResultRow(
                source=d.source, game_id=d.game_id, title=d.title, draw_date=d.draw_date,
                numbers=numbers, extras=json.dumps(d.extras), raw=d.raw,
                jackpot_date=d.jackpot_date, jackpot_text=d.jackpot_text,
                jackpot_amount=d.jackpot_amount, cash_value_text=d.cash_value_text,
                cash_value_amount=d.cash_value_amount, first_seen_at=now, last_seen_at=now))
            session.flush()
            continue
        if row.numbers != numbers or _bare(json.loads(row.extras or "[]")) != _bare(d.extras):
            notes.append(f"{d.title}, draw of {nice_date(d.draw_date)}: first read as "
                         f"{_numbers_text(row.numbers.split(), json.loads(row.extras or '[]'))}, "
                         f"now {_numbers_text(d.numbers, d.extras)}. The first reading is kept.")
        row.last_seen_at = now
        row.jackpot_date = d.jackpot_date
        row.jackpot_text, row.jackpot_amount = d.jackpot_text, d.jackpot_amount
        row.cash_value_text, row.cash_value_amount = d.cash_value_text, d.cash_value_amount
    if old_news:
        # How "is the old scraper still running?" is told: from its data alone.
        set_setting(session, OLD_LAST_NEW, now.isoformat(), now)
    session.flush()
    return notes


def remember_feed_order(session, order: list[str], now: datetime) -> None:
    """PA's game ids in the order PA lists them, so the TVs follow it. Taken from
    the whole feed, so a game that couldn't be read this time keeps its place
    (and its last good draw stays on the TVs)."""
    if order:
        set_setting(session, FEED_ORDER, json.dumps(order), now)


def feed_order(session) -> list[str]:
    try:
        order = json.loads(get_setting(session, FEED_ORDER, "[]"))
    except ValueError:
        return []
    return [str(x) for x in order] if isinstance(order, list) else []


def _numbers_text(numbers: list[str], extras: list[dict]) -> str:
    return " ".join([" ".join(numbers)] + [f"· {e.get('label')} {e.get('value')}"
                                           for e in extras])


# --------------------------------------------------------------------------- #
# The side-by-side check for Powerball and Mega Millions
# --------------------------------------------------------------------------- #

def _int_list(numbers: str) -> list[int]:
    return [int(n) for n in numbers.split()]


def _bonus(row: DrawResultRow, code: str) -> int | None:
    for e in json.loads(row.extras or "[]"):
        if e.get("code") == code and str(e.get("value", "")).isdigit():
            return int(e["value"])
    return None


def compare(pa: DrawResultRow, old: DrawResultRow) -> tuple[bool, str]:
    """Same draw, two sources. ``matched`` is the main numbers (in order) and the
    bonus ball, compared as numbers. The jackpot and next draw are noted when
    they differ, but don't count: the two are read at different times of day and
    estimates move."""
    code = PBMM[pa.game_id][1]
    out = []
    matched = True
    if _int_list(pa.numbers) != _int_list(old.numbers):
        matched = False
        out.append(f"Numbers: PA {pa.numbers}, old scraper {old.numbers}.")
    pa_bonus, old_bonus = _bonus(pa, code), _bonus(old, code)
    if pa_bonus is None or old_bonus is None or pa_bonus != old_bonus:
        matched = False
        out.append(f"{EXTRA_NAMES[code]}: PA {pa_bonus if pa_bonus is not None else 'none'}, "
                   f"old scraper {old_bonus if old_bonus is not None else 'none'}.")
    if matched:
        out.insert(0, "Same numbers.")
    if pa.jackpot_amount != old.jackpot_amount:
        out.append(f"Jackpot: PA {pa.jackpot_text or 'none'}, old scraper "
                   f"{old.jackpot_text or 'none'} (read at different times).")
    if pa.jackpot_date != old.jackpot_date:
        out.append(f"Next draw: PA {pa.jackpot_date or 'none'}, old scraper "
                   f"{old.jackpot_date or 'none'}.")
    return matched, " ".join(out)


def _by_date(session, source: str, game_id: str) -> dict[str, DrawResultRow]:
    rows = session.scalars(select(DrawResultRow).filter_by(source=source, game_id=game_id))
    return {r.draw_date: r for r in rows}


def compare_and_switch(session, now: datetime) -> dict:
    """Compare every Powerball/Mega Millions draw both sources have that hasn't
    been compared yet; report mismatches; switch the TVs to PA's feed once the
    trial is passed. Returns what happened (for the log and tests)."""
    source = pbmm_source(session)
    new_checks: list[DrawCheckRow] = []
    readings = {}
    for game_id in PBMM:
        pa = _by_date(session, PA_FEED, game_id)
        old = _by_date(session, OLD_SCRAPER, game_id)
        done = set(session.scalars(select(DrawCheckRow.draw_date).filter_by(game_id=game_id)))
        for draw_date in sorted((set(pa) & set(old)) - done):
            matched, detail = compare(pa[draw_date], old[draw_date])
            check = DrawCheckRow(game_id=game_id, draw_date=draw_date, matched=matched,
                                 detail=detail, checked_at=now)
            session.add(check)
            new_checks.append(check)
            readings[(game_id, draw_date)] = (pa[draw_date], old[draw_date])
    session.flush()

    in_trial = source == OLD_SCRAPER
    if new_checks and in_trial and not get_setting(session, TRIAL_STARTED):
        set_setting(session, TRIAL_STARTED, now.isoformat(), now)
    mismatches = [c for c in new_checks if not c.matched]
    if mismatches and in_trial:
        set_setting(session, TRIAL_STARTED, now.isoformat(), now)
    # Told once all of it is saved: a message Telegram doesn't take is tried
    # again later on its own, without checking the draw or restarting the trial
    # a second time.
    told = []
    for c in mismatches:
        pa, old = readings[(c.game_id, c.draw_date)]
        name, _ = PBMM[c.game_id]
        after = ("The TVs keep using the old scraper, and the 14-day check starts over."
                 if in_trial else
                 "The TVs keep using PA's feed (they already switched); nothing "
                 "changes on its own.")
        told.append((f"mismatch {c.game_id} {c.draw_date}",
                     f"⚠️ <b>{esc(name)}</b>, draw of {nice_date(c.draw_date)}: PA's feed "
                     f"and the old scraper disagree.\n"
                     f"PA's feed: {esc(_numbers_text(pa.numbers.split(), json.loads(pa.extras)))}\n"
                     f"Old scraper: {esc(_numbers_text(old.numbers.split(), json.loads(old.extras)))}\n"
                     f"{after}"))
    for event, text in told:
        tell(session, event, text, now)

    switched = False
    if in_trial:
        ready, counts = trial_passed(session, now)
        if ready:
            set_setting(session, PBMM_SOURCE, PA_FEED, now)
            set_setting(session, SWITCHED_AT, now.isoformat(), now)
            switched = True
            tell(session, "switched",
                 f"✅ Powerball and Mega Millions on the TVs now come from PA's own feed. "
                 f"For {TRIAL_DAYS} days every draw matched the old scraper exactly "
                 f"({counts[POWERBALL]} Powerball and {counts[MEGA_MILLIONS]} Mega Millions "
                 f"draws), so the old scraper isn't needed any more.", now)
    return {"checked": len(new_checks), "mismatches": len(mismatches), "switched": switched}


def trial_passed(session, now: datetime) -> tuple[bool, Counter]:
    """All of: at least 14 days since the trial started, every comparison since
    then matched, and at least 4 matched draws of each game since then.

    The mismatch that restarted the trial is stamped with the same moment the
    trial restarted from, so it isn't counted against the new trial."""
    start = _from_iso(get_setting(session, TRIAL_STARTED))
    counts: Counter = Counter()
    if start is None:
        return False, counts
    for c in session.scalars(select(DrawCheckRow).where(DrawCheckRow.game_id.in_(list(PBMM)))):
        at = aware(c.checked_at)
        if at < start:
            continue
        if not c.matched:
            if at > start:
                return False, counts
            continue
        counts[c.game_id] += 1
    ready = (now - start >= timedelta(days=TRIAL_DAYS)
             and all(counts[g] >= TRIAL_DRAWS for g in PBMM))
    return ready, counts


def check_old_scraper(session, now: datetime) -> None:
    """Until the switch, say so (once) if the old scraper hasn't sent a new
    Powerball/Mega Millions draw for over 3 days: the check can't finish without
    it, and the TVs still show its numbers. (Between them the two games draw five
    days a week.) That includes before the first comparison: a file that was
    already stuck when PA's feed was first read never gives one."""
    if pbmm_source(session) != OLD_SCRAPER:
        return
    last = old_last_new(session)
    if last is None:
        return
    if now - last > OLD_SCRAPER_QUIET:
        report(session, "old_quiet",
               f"⚠️ The old Powerball/Mega Millions scraper hasn't sent a new draw since "
               f"{nice_time(last)}. The side-by-side check with PA's feed is waiting on it.",
               now, key=last.isoformat())
    else:
        report(session, "old_quiet", None, now)


# --------------------------------------------------------------------------- #
# Telling the owner to turn the old scraper OFF, or back ON
# --------------------------------------------------------------------------- #
#
# The old scraper's daily run lives in another repo, which this job can't
# change, so the owner flips it by hand. Whether it's running is told from its
# data alone: a draw newer than any it showed before, within the last 4 days.
# (Powerball draws Mon/Wed/Sat and Mega Millions Tue/Fri, so a daily scraper
# always has something new within 4 days.)

def _today_eastern(now: datetime):
    try:
        from zoneinfo import ZoneInfo
        return aware(now).astimezone(ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001 — no time zone data: UTC's date is close enough
        return aware(now).date()


def _draw_day(now: datetime):
    """Today, for counting how old a draw is. Powerball and Mega Millions draw
    around 11pm Eastern and PA posts the numbers after that, often after
    midnight, so until 6am it's still the night before. (Otherwise Friday's Mega
    Millions draw would be "5 days old" at midnight on Tuesday night, before
    PA had a chance to post Tuesday's, and the owner would get a false alarm.)"""
    return _today_eastern(aware(now) - DRAW_NIGHT)


def old_last_new(session) -> datetime | None:
    """The last time the old scraper showed a draw newer than any before it."""
    last = _from_iso(get_setting(session, OLD_LAST_NEW))
    if last is None:   # saved before this was remembered: its newest row will do
        last = aware(session.scalar(select(func.max(DrawResultRow.first_seen_at))
                                    .filter_by(source=OLD_SCRAPER)))
    return last


def old_scraper_running(session, now: datetime) -> bool:
    last = old_last_new(session)
    return last is not None and now - last <= OLD_RUNNING


def note_pa_feed(session, ok: bool, now: datetime) -> None:
    """Remember since when PA's feed has failed to give Powerball and Mega
    Millions (couldn't be reached, couldn't be read, or either game left out);
    a good read clears it."""
    if ok:
        set_setting(session, PA_FAILING_SINCE, "", now)
    elif get_setting(session, PA_FAILING_SINCE) is None:
        set_setting(session, PA_FAILING_SINCE, now.isoformat(), now)


def pa_trouble(session, now: datetime) -> dict[str, str]:
    """Why PA's feed can't be trusted for Powerball/Mega Millions right now, in
    plain words, keyed by what kind of problem it is (empty: it's fine)."""
    out: dict[str, str] = {}
    today = _draw_day(now)
    for game_id, (name, _) in PBMM.items():
        newest = session.scalar(select(func.max(DrawResultRow.draw_date))
                                .filter_by(source=PA_FEED, game_id=game_id))
        if newest is None:
            out[f"stale:{game_id}"] = f"PA's feed has no {name} draw at all."
            continue
        age = (today - datetime.strptime(newest, "%Y-%m-%d").date()).days
        if age > PA_STALE_DAYS:
            out[f"stale:{game_id}"] = (f"PA's newest {name} draw is from {nice_date(newest)}, "
                                       f"{age} days ago: draws have been missed.")
    since = _from_iso(get_setting(session, PA_FAILING_SINCE))
    if since is not None and now - since > PA_FAILING:
        out["failing"] = (f"PA's number feed has been failing since {nice_time(since)} "
                          f"(over {int(PA_FAILING.total_seconds() // 3600)} hours).")
    switched_at = _from_iso(get_setting(session, SWITCHED_AT))
    for game_id, (name, _) in PBMM.items():
        latest = session.scalar(select(DrawCheckRow).filter_by(game_id=game_id)
                                .order_by(DrawCheckRow.draw_date.desc()).limit(1))
        if latest is None or latest.matched:
            continue
        if switched_at is not None and aware(latest.checked_at) < switched_at:
            continue
        out[f"mismatch:{game_id}"] = (f"PA's feed and the old scraper disagree on {name}, "
                                      f"draw of {nice_date(latest.draw_date)}.")
    return out


def _turn_on_message(reasons: dict[str, str]) -> str:
    return "\n".join(
        ["⚠️ <b>Turn the old scraper back ON</b>",
         "PA's feed can't be trusted for Powerball/Mega Millions right now:"]
        + [f"• {esc(reasons[k])}" for k in sorted(reasons)]
        + ["", OLD_SCRAPER_STEPS.format(action="Enable") + ". (If it's still on, leave it on.)",
           "While it runs, the TVs show its Powerball/Mega Millions numbers whenever "
           "they're newer than PA's. A note follows when PA's feed is fine again."])


def _turn_off_message() -> str:
    return "\n".join(
        ["🔌 <b>Turn the old Powerball/Mega Millions scraper OFF</b>",
         "The TVs use PA's own feed now, so it isn't needed any more.",
         OLD_SCRAPER_STEPS.format(action="Disable") + ".",
         "(A reminder comes once a week while it still looks like it's running.)"])


def advise_old_scraper(session, now: datetime) -> dict:
    """After the switch: tell the owner to turn the old scraper back ON when PA's
    feed can't be trusted (once per distinct problem, and a note when it's fine
    again), otherwise to turn it OFF while it still looks like it's running (at
    most once a week). Returns what was said (for the log and tests)."""
    said = {"on": False, "fine": False, "off": False}
    if pbmm_source(session) != PA_FEED:
        return said
    reasons = pa_trouble(session, now)
    try:
        told = set(json.loads(get_setting(session, OLD_ON_TOLD, "[]")))
    except ValueError:
        told = set()
    if reasons:
        if set(reasons) <= told:
            # Nothing new. Remember only what's still wrong, so a problem
            # that clears and later comes back is news again.
            if set(reasons) != told:
                set_setting(session, OLD_ON_TOLD, json.dumps(sorted(reasons)), now)
            print("Turn the old scraper back ON (already said): "
                  + " ".join(reasons[k] for k in sorted(reasons)), flush=True)
            return said
        _send_and_remember(session, OLD_ON_TOLD, json.dumps(sorted(reasons)),
                           _turn_on_message(reasons), now)
        said["on"] = True
        return said
    if told:
        set_setting(session, OLD_ON_TOLD, "", now)
        # That note says to turn it OFF again: the weekly reminder starts from here.
        set_setting(session, OLD_OFF_TOLD, now.isoformat(), now)
        session.commit()            # saved before the note goes (see _send_and_remember)
        send(FIXED["old_on"])
        said["fine"] = True
        return said
    last_told = _from_iso(get_setting(session, OLD_OFF_TOLD))
    if old_scraper_running(session, now) and (last_told is None
                                              or now - last_told >= OFF_REMINDER):
        _send_and_remember(session, OLD_OFF_TOLD, now.isoformat(), _turn_off_message(), now)
        said["off"] = True
    return said


# --------------------------------------------------------------------------- #
# Order lists
# --------------------------------------------------------------------------- #

def pdf_hash(pdf_bytes: bytes) -> str:
    return hashlib.sha256(pdf_bytes).hexdigest()


def saved_order_list(session, url: str) -> OrderListRow | None:
    """The saved list that came from this exact link, if any."""
    return session.scalar(select(OrderListRow).filter_by(url=url))


def save_order_list(session, order_list: OrderList, url: str, pdf_bytes: bytes,
                    now: datetime) -> str:
    """Save a week's list, once per "As of" date. Returns ``"saved"``,
    ``"same"`` (already saved, same PDF) or ``"different"`` (that As-of date is
    saved from a different PDF: nothing is saved, the first one is kept)."""
    row = session.get(OrderListRow, order_list.as_of)
    if row is not None:
        return "same" if row.pdf_sha256 == pdf_hash(pdf_bytes) else "different"
    session.add(OrderListRow(as_of=order_list.as_of, url=url, pdf_sha256=pdf_hash(pdf_bytes),
                             pdf=pdf_bytes, games=len(order_list.games), fetched_at=now))
    for g in order_list.games:
        session.add(OrderGameRow(as_of=order_list.as_of, game_number=g.game_number,
                                 price=g.price, tickets_per_pack=g.tickets_per_pack,
                                 name=g.name))
    session.flush()
    return "saved"


def order_games(session, as_of: str) -> list[OrderGameRow]:
    return list(session.scalars(select(OrderGameRow).filter_by(as_of=as_of)))


def order_change_message(session, order_list: OrderList) -> str | None:
    """What to tell the owner about a newly saved list: the first list, just the
    count; after that, the games added and dropped since the list before it.
    None when no game came or went."""
    before = session.scalar(select(OrderListRow.as_of)
                            .where(OrderListRow.as_of < order_list.as_of)
                            .order_by(OrderListRow.as_of.desc()).limit(1))
    when = nice_date(order_list.as_of)
    if before is None:
        return (f"📋 <b>PA's scratch-off order list</b> (as of {when}): "
                f"{len(order_list.games)} games you can order.")
    old = {g.game_number: g for g in order_games(session, before)}
    new = {g.game_number: g for g in order_list.games}
    added = [new[n] for n in new if n not in old]
    dropped = [old[n] for n in old if n not in new]
    if not added and not dropped:
        return None
    added.sort(key=lambda g: (g.price, g.game_number))
    dropped.sort(key=lambda g: (g.price, g.game_number))
    lines = [f"📋 <b>PA's new scratch-off order list</b> (as of {when}): "
             f"{len(order_list.games)} games."]
    if added:
        lines += ["", f"🆕 Added ({len(added)})"]
        lines += [f"{telegram._price(g.price)} {telegram._name(g)}" for g in added]
    if dropped:
        lines += ["", f"🚫 Off the list: PA no longer ships these ({len(dropped)})"]
        lines += [f"{telegram._price(g.price)} {telegram._name(g)}" for g in dropped]
    return "\n".join(lines)
