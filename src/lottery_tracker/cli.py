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
from .model import estimate_top_prize_totals, merge_games
from .notify import render_report, send_email, write_outputs
from .rules import Severity, evaluate
from .state import load_originals, load_state, save_history, save_originals, save_state

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
SAMPLES_DIR = ROOT / "samples"


def _load_html(offline: bool) -> tuple[str, str, str]:
    """Return (active_html, remaining_html, ended_html)."""
    if offline:
        active = (SAMPLES_DIR / "active.html").read_text()
        remaining = (SAMPLES_DIR / "remaining.html").read_text()
        ended = (SAMPLES_DIR / "sales_ended.html").read_text()
        return active, remaining, ended
    return fetch.fetch_active(), fetch.fetch_remaining(), fetch.fetch_sales_ended()


def _enrich_with_originals(current, inventory, originals, *, offline, save_html):
    """Populate true original top-prize counts (and odds) for inventory games.

    Fetches each carried game's detail page once, parses "offers N Top Prizes…",
    and caches it in ``originals`` keyed by game number. Already-cached games and
    games without a detail link are skipped. Detail fetch failures are non-fatal —
    the game just falls back to the estimated total.
    """
    for num in sorted(inventory):
        g = current.get(num)
        if g is None:
            continue
        cached = originals.get(num)
        if cached is None and g.detail_id:
            html = None
            if offline:
                p = SAMPLES_DIR / f"detail_{g.detail_id}.html"
                html = p.read_text() if p.exists() else None
            else:
                try:
                    html = fetch.fetch_detail(g.detail_id)
                    if save_html:
                        SAMPLES_DIR.mkdir(exist_ok=True)
                        (SAMPLES_DIR / f"detail_{g.detail_id}.html").write_text(html)
                except Exception as e:  # noqa: BLE001
                    print(f"WARNING: detail fetch failed for #{num} (id={g.detail_id}): {e}",
                          file=sys.stderr)
            if html is not None:
                info = parse.parse_detail(html)
                cached = {"detail_id": g.detail_id, **info}
                originals[num] = cached
        if cached and cached.get("top_prizes_original") is not None:
            g.top_prizes_total = cached["top_prizes_original"]
            g.total_is_estimate = False
            g.odds = cached.get("odds")


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
    _enrich_with_originals(current, cfg.inventory, originals, offline=args.offline,
                           save_html=args.save_html)
    save_originals(DATA_DIR / "originals.json", originals)

    # Fall back to the highest-ever-seen estimate for any game without a true count.
    estimate_top_prize_totals(current, previous)

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
        baseline=baseline,
    )
    paths = write_outputs(report_md, alerts, reports_dir=REPORTS_DIR, captured_at=captured_at)

    # Persist the new snapshot only AFTER a successful evaluate, so a crashed run
    # doesn't swallow a transition we never reported.
    save_state(state_path, current, captured_at=captured_at)
    save_history(DATA_DIR / "history", current, captured_at=captured_at)

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
