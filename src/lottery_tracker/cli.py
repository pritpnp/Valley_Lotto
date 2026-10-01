"""Command-line entrypoint: scrape -> parse -> diff -> alert -> report.

Usage:
    python -m lottery_tracker            # fetch live, evaluate, write reports
    python -m lottery_tracker --offline  # parse local HTML in ./samples instead of the network
    python -m lottery_tracker --now 2026-06-26T13:00:00Z   # pin the timestamp (for tests/CI)

Exit code is 0 normally, and 2 if there are CRITICAL alerts (a game you carry
ended) — handy for failing/branching a CI step.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import fetch, parse
from .config import Config
from .model import merge_games, update_change_tracking
from .notify import render_html, render_report, send_email, write_outputs
from .rules import Severity, evaluate
from .state import (
    append_scrape_log, content_hash, load_originals, load_state, save_originals,
    save_raw_html, save_snapshot, save_state, slugify,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
DOCS_DIR = ROOT / "docs"
SAMPLES_DIR = ROOT / "samples"


def _load_html(offline: bool) -> tuple[str, str, str]:
    """Return (active_html, remaining_html, ended_html)."""
    if offline:
        active = (SAMPLES_DIR / "active.html").read_text()
        remaining = (SAMPLES_DIR / "remaining.html").read_text()
        ended = (SAMPLES_DIR / "sales_ended.html").read_text()
        return active, remaining, ended
    return fetch.fetch_active(), fetch.fetch_remaining(), fetch.fetch_sales_ended()


def _enrich_with_originals(current, targets, originals, *, offline, save_html,
                           max_new_fetches=10_000):
    """Populate true original prize counts + odds for each target game.

    Fetches each game's detail page (odds + bulletin link) and its PA Bulletin
    (full prize structure) once, then caches it forever in ``originals`` keyed by
    game number. ``max_new_fetches`` caps how many *new* games we fetch this run so
    a large first-time catalog fill spreads over a few runs instead of one burst.
    Failures are non-fatal — the game just falls back to estimates.
    """
    import time

    def _get(url, sample_name):
        if offline:
            p = SAMPLES_DIR / sample_name
            return p.read_text() if p.exists() else None
        html = fetch.fetch(url)
        if save_html:
            SAMPLES_DIR.mkdir(exist_ok=True)
            (SAMPLES_DIR / sample_name).write_text(html)
        return html

    from .state import load_game_page, save_game_page
    pages = DATA_DIR / "pa_pages"

    def _read(num, cached, dhtml, bhtml):
        """Read the prize structure from a game's PA pages into the cache."""
        info = parse.parse_detail(dhtml)
        cached = dict(cached or {})
        cached.update({"detail_id": current[num].detail_id, **info})
        if bhtml is not None:
            # Replace the old reading wholesale, so nothing a newer reader
            # dropped lingers on from an older one.
            for k in ("prize_originals", "tickets_printed", "payout_pct"):
                cached.pop(k, None)
            cached.update(parse.parse_bulletin(bhtml))
            cached["parser_version"] = parse.PARSER_VERSION
        # Without the Bulletin this run, what was read before stays as it was,
        # and the version stays old so it's tried again next run.
        return cached

    new_fetches = 0
    for num in sorted(targets):
        g = current.get(num)
        if g is None:
            continue
        cached = originals.get(num)
        stale = (cached or {}).get("parser_version") != parse.PARSER_VERSION
        dsaved = load_game_page(pages, num, "detail")
        bsaved = load_game_page(pages, num, "bulletin")

        # The reader improved and the pages are on file: re-read, no network.
        if cached and stale and dsaved is not None:
            originals[num] = cached = _read(num, cached, dsaved, bsaved)

        # Not read yet, missing its Bulletin, or read by an older reader with no
        # pages on file: fetch them (and keep them this time).
        need = (cached is None or not cached.get("prize_originals")
                or (cached.get("parser_version") != parse.PARSER_VERSION))
        if need and g.detail_id and new_fetches < max_new_fetches:
            new_fetches += 1
            try:
                dhtml = _get(fetch.DETAIL_URL.format(id=g.detail_id), f"detail_{g.detail_id}.html")
                if dhtml is not None:
                    if not offline:
                        save_game_page(pages, num, "detail", dhtml)
                    b_url = parse.parse_detail(dhtml).get("bulletin_url")
                    bhtml = _get(b_url, f"bulletin_{g.detail_id}.html") if b_url else None
                    if bhtml is not None and not offline:
                        save_game_page(pages, num, "bulletin", bhtml)
                    originals[num] = cached = _read(num, cached, dhtml, bhtml)
                if not offline:
                    time.sleep(0.2)  # be polite to PA's servers
            except Exception as e:  # noqa: BLE001
                print(f"WARNING: detail/bulletin fetch failed for #{num}: {e}", file=sys.stderr)
        # Apply whatever we have to the game.
        if cached:
            if cached.get("prize_originals"):
                g.tier_originals = cached["prize_originals"]
                g.tier_originals_other = cached.get("prize_other") or {}
                g.tickets_printed = cached.get("tickets_printed")
                g.payout_pct = cached.get("payout_pct")
            # PA's published odds only. Odds worked out from the prize table
            # would make the table "agree with itself" in the trust check.
            g.odds = cached.get("odds") or g.odds
        # Top prizes printed: the count for the same dollar value as the top
        # prize PA lists, or unknown. Never a count for some other prize, and
        # never an estimate.
        pair = g.top_prize_pair
        g.top_prizes_total = pair[1] if pair else None
        g.total_is_estimate = pair is None
    if new_fetches >= max_new_fetches:
        print(f"Reached max_new_fetches ({max_new_fetches}); remaining games fill next run.",
              file=sys.stderr)


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lottery_tracker", description="PA scratch-off tracker")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--offline", action="store_true",
                    help="parse HTML from ./samples instead of fetching the network")
    ap.add_argument("--now", default=None, help="ISO timestamp to stamp this run (default: now, UTC)")
    ap.add_argument("--no-email", action="store_true", help="skip email even if SMTP_* is set")
    ap.add_argument("--save-html", action="store_true",
                    help="also save the fetched HTML into ./samples (useful first run)")
    args = ap.parse_args(argv)

    captured_at = args.now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cfg = Config.load(args.config)

    try:
        active_html, remaining_html, ended_html = _load_html(args.offline)
    except fetch.FetchError as e:
        # PA's site being slow or down is not this program being broken, and a
        # scheduled run shouldn't cry wolf over it: the next run picks it up.
        # EX_TEMPFAIL (75) says "try again later" and the workflow treats it as
        # a warning. Anything genuinely wrong still exits non-zero and fails.
        print(f"PA's website did not answer: {e}", file=sys.stderr)
        print("Nothing was written. The next scheduled run will try again.",
              file=sys.stderr)
        return 75

    if args.save_html and not args.offline:
        SAMPLES_DIR.mkdir(exist_ok=True)
        (SAMPLES_DIR / "active.html").write_text(active_html)
        (SAMPLES_DIR / "remaining.html").write_text(remaining_html)
        (SAMPLES_DIR / "sales_ended.html").write_text(ended_html)

    active = parse.parse_active(active_html)
    remaining = parse.parse_remaining(remaining_html)
    ended = parse.parse_sales_ended(ended_html)
    current = merge_games(remaining, ended, active)

    state_path = DATA_DIR / "state.json"
    previous = load_state(state_path)

    # True original prize counts come from each game's detail page. Fetch them
    # once for the games we carry and cache forever (originals never change).
    originals = load_originals(DATA_DIR / "originals.json")
    # Inventory always; with scout_catalog, every active game too (cached forever).
    targets = set(cfg.inventory)
    if cfg.scout_catalog:
        targets |= {n for n, g in current.items() if g.status == "active"}
    _enrich_with_originals(current, targets, originals, offline=args.offline,
                           save_html=args.save_html, max_new_fetches=cfg.max_new_fetches)
    save_originals(DATA_DIR / "originals.json", originals)

    # No estimates: a top-prize count is either matched to PA's printed count for
    # the same prize, or left unknown (see _enrich_with_originals).

    # Track when each game's data last actually moved (for the "last move" display).
    update_change_tracking(current, previous, captured_at)

    # First-ever run: nothing to diff against, so seed the baseline silently
    # instead of alerting on every historically-ended game.
    baseline = len(previous) == 0
    if baseline:
        alerts = []
        print(f"Baseline established: tracking {len(current)} games. No alerts on first run.",
              file=sys.stderr)
    else:
        alerts = evaluate(
            current,
            previous,
            inventory=cfg.inventory,
            thresholds=cfg.thresholds,
            report_all_games=cfg.report_all_games,
        )

    report_md = render_report(
        alerts, current,
        inventory=cfg.inventory, thresholds=cfg.thresholds, weights=cfg.rating_weights,
        captured_at=captured_at, baseline=baseline, previous=previous,
        bring_in_min_left=cfg.bring_in_min_left, bring_in_per_price=cfg.bring_in_per_price,
    )
    paths = write_outputs(report_md, alerts, reports_dir=REPORTS_DIR, captured_at=captured_at)

    # No public dashboard page any more. This repo is public, and a page about
    # a store's games belongs behind the app's login, where the app's dashboard
    # reads them live from the database. PA's own data is still collected and
    # saved below, as before.

    # Persist the new snapshot only AFTER a successful evaluate, so a crashed run
    # doesn't swallow a transition we never reported.
    save_state(state_path, current, captured_at=captured_at)

    # Archive EVERY scrape in an append-only log (never deleted). Only store a full
    # snapshot + raw HTML when the data actually changed (even by one number);
    # identical scrapes are recorded in the log but not duplicated on disk.
    slug = slugify(captured_at)
    chash = content_hash(current)
    changed = baseline or (chash != content_hash(previous))
    append_scrape_log(DATA_DIR / "scrape_log.jsonl", {
        "captured_at": captured_at, "slug": slug, "hash": chash,
        "changed": changed, "games": len(current),
    })
    if changed:
        save_snapshot(DATA_DIR / "snapshots", slug, current, captured_at=captured_at)
        if not args.offline:
            save_raw_html(DATA_DIR / "raw", slug, {
                "active": active_html, "remaining": remaining_html, "sales_ended": ended_html,
            })
        print(f"Data changed -> stored snapshot {slug}", file=sys.stderr)
    else:
        print(f"Identical to previous scrape -> archived in scrape_log only ({slug})",
              file=sys.stderr)

    if alerts and not args.no_email:
        crit = sum(1 for a in alerts if a.severity == Severity.CRITICAL)
        subject = f"[Valley Lotto] {len(alerts)} alert(s)" + (f" — {crit} critical" if crit else "")
        try:
            send_email(subject, report_md)
        except Exception as e:  # noqa: BLE001 — email must never crash the run
            print(f"WARNING: email failed: {e}", file=sys.stderr)

    print(report_md)
    print(f"\nWrote: {paths['latest']}  |  alerts: {len(alerts)}  |  games tracked: {len(current)}")

    return 2 if any(a.severity == Severity.CRITICAL for a in alerts) else 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
