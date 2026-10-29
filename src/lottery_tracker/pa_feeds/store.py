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
  website that doesn't answer is only mentioned once it has kept failing for
  over an hour.
* **The side-by-side check.** Powerball and Mega Millions are read from PA's
  feed and from the old scraper; every draw is compared. After 14 days of exact
  matches (at least 4 draws of each game) the TVs switch to PA's feed, for good.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter
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

# The games both sources cover: id -> (name, bonus ball code).
PBMM = {POWERBALL: ("Powerball", "PB"), MEGA_MILLIONS: ("Mega Millions", "MB")}

TRIAL_DAYS = 14
TRIAL_DRAWS = 4                        # matched draws of EACH game during the trial
OLD_SCRAPER_QUIET = timedelta(days=3)
FETCH_GRACE = timedelta(hours=1)

# What the owner is told when a problem clears. None: it clears quietly.
FIXED = {
    "numbers_fetch": "✅ PA's number feed is answering again.",
    "numbers_format": "✅ PA's number feed reads normally again.",
    "old_fetch": "✅ The old Powerball/Mega Millions scraper's file can be read again.",
    "old_format": "✅ The old Powerball/Mega Millions scraper's file reads normally again.",
    "old_quiet": "✅ The old scraper is sending Powerball/Mega Millions draws again.",
    "orders_page_fetch": "✅ PA's retailer page is answering again.",
    "orders_pdf_fetch": "✅ PA's order list downloads again.",
    "orders_format": "✅ PA's order list reads normally again.",
    "orders_check": None,
    "orders_conflict": None,
}


def create_tables(engine) -> None:
    """Create the five ``pa_`` tables if missing (nothing else), and turn on
    row-level security so Supabase's public API can't reach them."""
    Base.metadata.create_all(engine, tables=PA_TABLES)
    migrate.enable_rls(engine)


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
        if send(problem) or not telegram.configured():
            set_setting(session, setting, signature, now)
        return True
    if before:
        set_setting(session, setting, "", now)
        if FIXED.get(slot):
            send(FIXED[slot])
            return True
    return False


def problem_list(title: str, problems: list[str]) -> str | None:
    """``⚠️ title`` and one line per problem (plain text, escaped here)."""
    if not problems:
        return None
    return "\n".join([f"⚠️ <b>{esc(title)}</b>"]
                     + [f"• {esc(p)}" for p in problems])


def fetch_failed(session, name: str, what: str, error: str, now: datetime, *,
                 meanwhile: str = "") -> bool:
    """A download failed. Remember when the failures started; only tell the
    owner once they've gone on for over an hour (a blip isn't news).
    ``meanwhile`` says what happens in the meantime, in plain words."""
    since = _from_iso(get_setting(session, f"failing_since:{name}"))
    if since is None:
        set_setting(session, f"failing_since:{name}", now.isoformat(), now)
        print(f"{what}: {error} (first failure; will mention it after an hour)", flush=True)
        return False
    if now - since <= FETCH_GRACE:
        print(f"{what}: {error} (failing since {nice_time(since)})", flush=True)
        return False
    text = (f"⚠️ Can't reach {esc(what)}: it has kept failing since "
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


def save_draws(session, draws: list[Draw], now: datetime) -> list[str]:
    """Save each draw once per source; later readings of the same draw refresh
    ``last_seen_at`` and the jackpot fields. Returns a note for each draw whose
    numbers now read differently from what was first saved (the first reading is
    kept, and the owner is told, since which one is right can't be guessed)."""
    notes = []
    for d in draws:
        row = session.scalar(select(DrawResultRow).filter_by(
            source=d.source, game_id=d.game_id, draw_date=d.draw_date))
        numbers = " ".join(d.numbers)
        if row is None:
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
    for c in mismatches:
        pa, old = readings[(c.game_id, c.draw_date)]
        name, _ = PBMM[c.game_id]
        after = ("The TVs keep using the old scraper, and the 14-day check starts over."
                 if in_trial else
                 "The TVs keep using PA's feed (they already switched); nothing "
                 "changes on its own.")
        send(f"⚠️ <b>{esc(name)}</b>, draw of {nice_date(c.draw_date)}: PA's feed "
             f"and the old scraper disagree.\n"
             f"PA's feed: {esc(_numbers_text(pa.numbers.split(), json.loads(pa.extras)))}\n"
             f"Old scraper: {esc(_numbers_text(old.numbers.split(), json.loads(old.extras)))}\n"
             f"{after}")
    if mismatches and in_trial:
        set_setting(session, TRIAL_STARTED, now.isoformat(), now)

    switched = False
    if in_trial:
        ready, counts = trial_passed(session, now)
        if ready:
            set_setting(session, PBMM_SOURCE, PA_FEED, now)
            set_setting(session, SWITCHED_AT, now.isoformat(), now)
            switched = True
            send(f"✅ Powerball and Mega Millions on the TVs now come from PA's own feed. "
                 f"For {TRIAL_DAYS} days every draw matched the old scraper exactly "
                 f"({counts[POWERBALL]} Powerball and {counts[MEGA_MILLIONS]} Mega Millions "
                 f"draws), so the old scraper isn't needed any more.")
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
    """During the trial, say so (once) if the old scraper hasn't sent a new
    Powerball/Mega Millions draw for over 3 days: the check can't finish without
    it. (Between them the two games draw five days a week.)"""
    if pbmm_source(session) != OLD_SCRAPER or not get_setting(session, TRIAL_STARTED):
        return
    last = aware(session.scalar(select(func.max(DrawResultRow.first_seen_at))
                                .filter_by(source=OLD_SCRAPER)))
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
