"""Daily (and per-count) sales reporting from the scan log.

A "count" is a walk of the counter where a clerk scans the current top ticket of
every box. Stores run 2-3 counts a day — a **morning**, optional **midday**, and
**night** count. Sold between two counts = the ticket movement in each box; sold
for the day = first count to last, bridging any pack changeover in between.

Boxes are labeled like ``A1..A24`` and ``B1..B24`` (two 24-count units = 48 boxes).
Each scan carries its ``slot`` and the game is read from the barcode, so the
report is naturally per-box AND per-game: "box A1 (game 1750) sold 15 → $450".

This module is pure: give it a :class:`~lottery_tracker.scans.ScanLog`, a date, a
price map and a pack-size resolver, and it returns numbers. Rendering to Markdown
(for the daily report file / email) is a thin function at the bottom.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .scans import ScanLog, compute_sold, sold_over_sequence, learn_pack_size, _slot_sort_key

# Canonical ordering of the named daily counts. Unknown labels sort last but keep
# their real timestamp order, so custom labels still work.
SESSION_ORDER = {"morning": 0, "midday": 1, "afternoon": 1, "evening": 2, "night": 2, "close": 3}


def as_zone(tz):
    """Accept a tz name, a tzinfo, or None."""
    if tz is None or isinstance(tz, ZoneInfo):
        return tz
    try:
        return ZoneInfo(str(tz))
    except Exception:  # noqa: BLE001 — a bad tz name must not break reporting
        return None


def business_date(iso: str, tz=None) -> str:
    """The LOCAL calendar date a scan belongs to.

    Scans are stamped in UTC (correct — it's unambiguous), but a store's "day"
    is local. In Pennsylvania a 9pm night count is already 1am UTC *tomorrow*, so
    matching on the raw UTC prefix would file it under the next day and split one
    day's sales across two reports. Convert before grouping.
    """
    zone = as_zone(tz)
    if not iso:
        return ""
    if zone is None:
        return iso[:10]
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(zone).strftime("%Y-%m-%d")
    except ValueError:
        return iso[:10]


def local_time(iso: str, tz=None) -> str:
    """HH:MM in the store's own timezone, for display."""
    zone = as_zone(tz)
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if zone is not None:
            dt = dt.astimezone(zone)
        return dt.strftime("%H:%M")
    except ValueError:
        return (iso or "")[11:16]


def _session_sort_key(scan):
    return (scan.scanned_at, SESSION_ORDER.get((scan.session or "").lower(), 99))


@dataclass
class Interval:
    """Sold between two consecutive counts within a box."""

    frm: str                       # e.g. "morning"
    to: str                        # e.g. "midday"
    sold: Optional[int]
    revenue: Optional[float]
    estimated: bool                # True if a pack changeover was bridged
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SlotDay:
    """One box's day: the counts taken, sold per interval, and the day total."""

    slot: Optional[str]
    game_number: str
    price: Optional[float]
    counts: list                   # [{session, scanned_at, pack, ticket}]
    intervals: list                # list[Interval]
    total_sold: int
    revenue: Optional[float]
    pack_size_used: Optional[int]
    estimated: bool
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["intervals"] = [i.to_dict() for i in self.intervals]
        return d


@dataclass
class DailyReport:
    date: str
    store: str
    rows: list                     # list[SlotDay], box order
    total_tickets: int
    total_revenue: Optional[float]
    per_game: dict                 # game_number -> {tickets, revenue}

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rows"] = [r.to_dict() for r in self.rows]
        return d


def _price_for(prices, game_number):
    if not prices:
        return None
    return prices.get(game_number) or prices.get(str(game_number))


def daily_report(log: ScanLog, date: str, *, prices: dict | None = None,
                 resolver=None, store: Optional[str] = None, tz=None) -> DailyReport:
    """Build a day's report for one store.

    ``date`` is a ``YYYY-MM-DD`` prefix matched against each scan's timestamp.
    ``prices`` maps game_number -> ticket price. ``resolver`` is a
    :class:`~lottery_tracker.packs.PackSizeResolver` (from ``config.pack_resolver()``);
    if omitted, pack size is learned from the log alone.
    """
    todays = [s for s in log.scans
              if business_date(s.scanned_at, tz) == date
              and (store is None or s.store == store)]
    store_label = store or (todays[0].store if todays else "default")

    # Group by box; fall back to a game-keyed bucket if a scan has no slot.
    groups: dict = {}
    for s in todays:
        key = s.slot or f"game:{s.game_number}"
        groups.setdefault(key, []).append(s)

    rows: list[SlotDay] = []
    for key in sorted(groups, key=lambda k: _slot_sort_key(k) if not k.startswith("game:") else ("Z", 0, k)):
        scans = sorted(groups[key], key=_session_sort_key)
        game = scans[-1].game_number            # the game currently in this box
        price = _price_for(prices, game)

        # Pack size: prefer the resolver (config + full-history learning); else
        # learn from this game's entire history in the log.
        observed = learn_pack_size(log.for_game(game))
        if resolver is not None:
            size = resolver.size_for(price, game_number=game, observed_max=observed).size
        else:
            size = observed

        # Per-interval sold (consecutive counts).
        intervals: list[Interval] = []
        for a, b in zip(scans, scans[1:]):
            r = compute_sold(a, b, price=price, pack_size=size)
            intervals.append(Interval(
                frm=a.session or a.scanned_at, to=b.session or b.scanned_at,
                sold=r.tickets_sold, revenue=r.revenue,
                estimated=not r.same_pack, note=r.note,
            ))

        # Day total (first count -> last), bridging rollovers.
        seq = sold_over_sequence(scans, price=price, pack_size=size)

        rows.append(SlotDay(
            slot=None if key.startswith("game:") else key,
            game_number=game, price=price,
            counts=[{"session": s.session, "scanned_at": s.scanned_at,
                     "at_local": local_time(s.scanned_at, tz),
                     "pack": s.pack, "ticket": s.ticket} for s in scans],
            intervals=intervals,
            total_sold=seq.tickets_sold, revenue=seq.revenue,
            pack_size_used=seq.pack_size_used, estimated=seq.estimated,
            notes=seq.notes,
        ))

    total_tickets = sum(r.total_sold for r in rows)
    have_prices = any(r.revenue is not None for r in rows)
    total_revenue = round(sum(r.revenue or 0 for r in rows), 2) if have_prices else None

    per_game: dict = {}
    for r in rows:
        g = per_game.setdefault(r.game_number, {"tickets": 0, "revenue": 0.0, "has_price": False})
        g["tickets"] += r.total_sold
        if r.revenue is not None:
            g["revenue"] += r.revenue
            g["has_price"] = True
    for g in per_game.values():
        g["revenue"] = round(g["revenue"], 2) if g.pop("has_price") else None

    return DailyReport(date=date, store=store_label, rows=rows,
                       total_tickets=total_tickets, total_revenue=total_revenue,
                       per_game=per_game)


def render_daily_report_md(report: DailyReport) -> str:
    """A compact Markdown daily report suitable for the report file / email."""
    lines = [f"# Daily scratch-off sales — {report.date}  ({report.store})", ""]
    rev = f" · ${report.total_revenue:,.2f}" if report.total_revenue is not None else ""
    lines.append(f"**Total: {report.total_tickets} tickets sold{rev}**")
    lines.append("")
    lines.append("| Box | Game | Sold | Revenue | Counts (ticket #) | Notes |")
    lines.append("|-----|------|-----:|--------:|-------------------|-------|")
    for r in report.rows:
        counts = " → ".join(
            f"{(c['session'] or c.get('at_local') or c['scanned_at'][11:16])}:{c['ticket']:03d}"
            for c in r.counts
        )
        rev = f"${r.revenue:,.2f}" if r.revenue is not None else "—"
        flags = []
        if r.estimated:
            flags.append("est. (pack change)")
        flags.extend(r.notes)
        lines.append(
            f"| {r.slot or '—'} | {r.game_number} | {r.total_sold} | {rev} | {counts} | {'; '.join(flags)} |"
        )
    return "\n".join(lines) + "\n"
