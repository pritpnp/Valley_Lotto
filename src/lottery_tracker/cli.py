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
from .model import estimate_top_prize_totals, merge_games, update_change_tracking
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

    new_fetches = 0
    for num in sorted(targets):
        g = current.get(num)
        if g is None:
            continue
        cached = originals.get(num)
        need_bulletin = not (cached or {}).get("prize_originals")
        if (cached is None or need_bulletin) and g.detail_id and new_fetches < max_new_fetches:
            new_fetches += 1
            try:
                dhtml = _get(fetch.DETAIL_URL.format(id=g.detail_id), f"detail_{g.detail_id}.html")
                if dhtml is not None:
                    info = parse.parse_detail(dhtml)
                    cached = {**(cached or {}), "detail_id": g.detail_id, **info}
                    b_url = info.get("bulletin_url")
                    if b_url and not cached.get("prize_originals"):
                        bhtml = _get(b_url, f"bulletin_{g.detail_id}.html")
                        if bhtml is not None:
                            cached.update(parse.parse_bulletin(bhtml))
                    originals[num] = cached
                if not offline:
                    time.sleep(0.2)  # be polite to PA's servers
            except Exception as e:  # noqa: BLE001
                print(f"WARNING: detail/bulletin fetch failed for #{num}: {e}", file=sys.stderr)
        # Apply whatever we have to the game.
        if cached:
            if cached.get("prize_originals"):
                g.tier_originals = cached["prize_originals"]
                g.tickets_printed = cached.get("tickets_printed")
                g.payout_pct = cached.get("payout_pct")
            if cached.get("top_prizes_original") is not None:
                g.top_prizes_total = cached["top_prizes_original"]
                g.total_is_estimate = False
            g.odds = cached.get("odds") or cached.get("odds_computed") or g.odds
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

    active_html, remaining_html, ended_html = _load_html(args.offline)
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

    # Fall back to the highest-ever-seen estimate for any game without a true count.
    estimate_top_prize_totals(current, previous)

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
        inventory=cfg.inventory, thresholds=cfg.thresholds, captured_at=captured_at,
        baseline=baseline, previous=previous,
        bring_in_min_left=cfg.bring_in_min_left, bring_in_per_price=cfg.bring_in_per_price,
    )
    paths = write_outputs(report_md, alerts, reports_dir=REPORTS_DIR, captured_at=captured_at)

    # GitHub Pages dashboard.
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "index.html").write_text(render_html(
        alerts, current, inventory=cfg.inventory, thresholds=cfg.thresholds,
        captured_at=captured_at, baseline=baseline, previous=previous,
        bring_in_min_left=cfg.bring_in_min_left, bring_in_per_price=cfg.bring_in_per_price,
    ))

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
