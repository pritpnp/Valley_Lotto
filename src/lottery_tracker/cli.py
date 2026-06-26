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
from .model import merge_games
from .notify import render_report, send_email, write_outputs
from .rules import Severity, evaluate
from .state import load_state, save_history, save_state

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
