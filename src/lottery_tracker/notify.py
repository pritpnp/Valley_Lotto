"""Render reports and send notifications.

Channels, in order of how much setup they need:

  * Markdown report + JSON snapshot in the repo — always written, no setup.
  * GitHub Issue — handled by the GitHub Action (it reads ``alerts.json``).
  * Email — opt-in: set SMTP_* environment variables and it turns on.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from .model import Game
from .rules import Alert, Severity, Thresholds, _is_low

_SEV_EMOJI = {Severity.CRITICAL: "🔴", Severity.WARN: "🟠", Severity.INFO: "🔵"}


def render_report(
    alerts: list[Alert],
    games: dict[str, Game],
    *,
    inventory: set[str],
    thresholds: Thresholds,
    captured_at: str,
    baseline: bool = False,
) -> str:
    """Human-readable Markdown: today's alerts + a health table for your games."""
    lines: list[str] = []
    lines.append(f"# Valley Lotto report — {captured_at}")
    lines.append("")

    if baseline:
        lines.append("## 🏁 Baseline established")
        lines.append("")
        lines.append(
            f"First run — recorded the current state of {len(games)} games as the "
            "starting point. No alerts this run; from now on you'll only be told "
            "about **changes** (games that newly end or whose prizes drop too low)."
        )
        lines.append("")
    elif alerts:
        lines.append(f"## ⚠️ {len(alerts)} new alert(s)")
        lines.append("")
        for a in alerts:
            tag = _SEV_EMOJI[a.severity]
            own = " **[YOUR GAME]**" if a.owned else ""
            lines.append(f"- {tag}{own} {a.message}")
        lines.append("")
    else:
        lines.append("## ✅ No new alerts")
        lines.append("")
        lines.append("Nothing ended and nothing crossed your low-prize threshold since the last run.")
        lines.append("")

    # Health table for the games you carry.
    owned_games = [games[n] for n in sorted(inventory) if n in games]
    if owned_games:
        lines.append("## Your games — current status")
        lines.append("")
        lines.append("| Game | # | Price | Odds | Top prize | Top prizes left | % left | Flag |")
        lines.append("|------|---|------:|------|-----------|----------------:|-------:|------|")
        for g in owned_games:
            pct = g.top_prize_pct_remaining
            # A "~" marks a percentage based on the estimated (not yet confirmed) original.
            pct_s = "—" if pct is None else (f"~{pct:.0%}" if g.total_is_estimate else f"{pct:.0%}")
            left = "—" if g.top_prizes_remaining is None else str(g.top_prizes_remaining)
            total = "" if g.top_prizes_total is None else f"/{g.top_prizes_total}"
            low, _ = _is_low(g, thresholds)
            if g.status == "ended":
                flag = "🔴 ENDED"
            elif low:
                flag = "🟠 SWAP"
            else:
                flag = "✅"
            price = "—" if g.price is None else f"${g.price:g}"
            lines.append(
                f"| {g.name} | {g.game_number} | {price} | {g.odds or '—'} | "
                f"{g.top_prize_value or '—'} | {left}{total} | {pct_s} | {flag} |"
            )
        lines.append("")
        lines.append(
            "> **% left** = top prizes still unclaimed ÷ original count (from the game's "
            "PA detail page). A `~` means the original is still estimated from the highest "
            "count seen so far. **🟠 SWAP** = below your threshold — time to switch the game out."
        )
        lines.append("")

    missing = sorted(n for n in inventory if n not in games)
    if missing:
        lines.append(
            "> Note: these inventory game numbers weren't found on any PA page "
            f"(check the numbers): {', '.join(missing)}"
        )
        lines.append("")

    return "\n".join(lines)


def write_outputs(
    report_md: str,
    alerts: list[Alert],
    *,
    reports_dir: str | Path,
    captured_at: str,
) -> dict[str, Path]:
    """Write the dated report, a 'latest' copy, and alerts.json for the Action."""
    d = Path(reports_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = captured_at.split("T")[0]
    dated = d / f"{stamp}.md"
    latest = d / "latest.md"
    alerts_json = d / "alerts.json"
    dated.write_text(report_md)
    latest.write_text(report_md)
    alerts_json.write_text(json.dumps([a.to_dict() for a in alerts], indent=2))
    return {"dated": dated, "latest": latest, "alerts_json": alerts_json}


# --------------------------------------------------------------------------- #
# Email (opt-in via environment variables)
# --------------------------------------------------------------------------- #
def email_configured() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "ALERT_TO"))


def send_email(subject: str, body_md: str) -> None:
    """Send the report by email. No-op if SMTP_* env vars aren't set.

    For Gmail, SMTP_HOST=smtp.gmail.com, SMTP_PORT=587, SMTP_USER=you@gmail.com,
    and SMTP_PASS = a Google *app password* (not your normal password).
    """
    if not email_configured():
        return
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to_addrs = [a.strip() for a in os.environ["ALERT_TO"].split(",") if a.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("ALERT_FROM", user)
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(body_md)

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.send_message(msg)
