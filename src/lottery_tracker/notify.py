"""Render reports and send notifications.

Channels, in order of how much setup they need:

  * Markdown report + JSON snapshot in the repo — always written, no setup.
  * GitHub Issue — handled by the GitHub Action (it reads ``alerts.json``).
  * Email — opt-in: set SMTP_* environment variables and it turns on.
"""

from __future__ import annotations

import html as _html
import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from .model import Game
from .rules import Alert, Severity, Thresholds, _is_low, recommendation

_SEV_EMOJI = {Severity.CRITICAL: "🔴", Severity.WARN: "🟠", Severity.INFO: "🔵"}


def _days_between(a_iso: str, b_iso: str) -> int:
    from datetime import datetime
    a = datetime.fromisoformat(a_iso.replace("Z", "+00:00"))
    b = datetime.fromisoformat(b_iso.replace("Z", "+00:00"))
    return (b - a).days


def last_move_label(g: Game, now_iso: str) -> str:
    """Human "last move" string: when we last saw this game's numbers change."""
    if g.last_changed:
        d = _days_between(g.last_changed, now_iso)
        return "moved today" if d <= 0 else f"moved {d}d ago"
    if g.first_seen:
        d = _days_between(g.first_seen, now_iso)
        return "just added" if d <= 0 else f"static {d}d"
    return "—"


def render_report(
    alerts: list[Alert],
    games: dict[str, Game],
    *,
    inventory: set[str],
    thresholds: Thresholds,
    captured_at: str,
    baseline: bool = False,
    previous: dict[str, Game] | None = None,
    bring_in_min_left: float = 0.6,
    bring_in_per_price: int = 4,
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

    # Health table for the games you carry — sorted by overall odds (best first),
    # because the odds are the real "will a customer win anything / break even?"
    # number and they barely move over a game's life.
    owned_games = [games[n] for n in sorted(inventory) if n in games]
    recs = {g.game_number: recommendation(g, thresholds) for g in owned_games}
    owned_games.sort(key=lambda g: (recs[g.game_number][0] != "send_back",
                                    -(g.price or 0), g.odds_value or 99))
    if owned_games:
        send = [g for g in owned_games if recs[g.game_number][0] == "send_back"]
        lines.append(f"## Recommendation: send back {len(send)}, keep {len(owned_games) - len(send)}")
        lines.append("")
        if send:
            lines.append("**🔴 Send back (stop selling / return):** "
                         + ", ".join(f"#{g.game_number} {g.name} (${g.price:g}, {recs[g.game_number][1]})"
                                     for g in send))
            lines.append("")
        lines.append("| Game | # | Price | Win odds | Prizes left | Density | Last move | Action |")
        lines.append("|------|---|------:|:-------:|:----------:|:-------:|:---------:|--------|")
        for g in owned_games:
            st = g.sell_through_pct if g.sell_through_pct is not None else g.top_prize_pct_remaining
            left_s = "—" if st is None else f"{min(1.0, st):.0%}"
            price = "—" if g.price is None else f"${g.price:g}"
            odds = f"1:{g.odds_value:g}" if g.odds_value is not None else "—"
            moved = last_move_label(g, captured_at)
            jd = g.jackpot_density
            jd_s = "—" if jd is None else f"{jd:.2f}"
            act, reason = recs[g.game_number]
            action = ("🔴 **SEND BACK**" if act == "send_back" else "🟢 KEEP") + f" — {reason}"
            lines.append(
                f"| {g.name} | {g.game_number} | {price} | {odds} | "
                f"{left_s} | {jd_s} | {moved} | {action} |"
            )
        lines.append("")
        lines.append(
            "> **Win odds (1:X) is the number that matters** — the published chance a ticket wins "
            "*any* prize. Because the cheap break-even prizes vastly outnumber the jackpots, this is "
            "effectively a *low-prize-weighted* figure: lower = better chance a customer wins something. "
            "**🔻 WEAK ODDS** = worse than 1:" f"{thresholds.weak_odds:g}" " (poor to stock). "
            "**Prizes left (est.)** = how much of the *whole* game is unsold — estimated from the top "
            "prizes, which works because prizes are shuffled evenly through the pack, so it tracks the "
            "cheap prizes too. **🟠 SWAP** = under 40% left (game mostly sold through). "
            "**Lower prizes left** = non-jackpot wins still in the pack. "
            "*A separate single-day \"low-prize %\" can't be computed — on any one day it's mathematically "
            "identical to the top-prize %, so we don't fake one.*"
        )
        lines.append("")

        # Per-game prize-tier breakdown (every published price), with the change
        # since the last scrape and bottom-to-top weights (cheapest prize heaviest).
        prev = previous or {}
        lines.append("## Prize tiers per game (cheapest weighted heaviest)")
        lines.append("")
        for g in owned_games:
            rows = g.tier_table(prev_tiers=(prev.get(g.game_number).prize_tiers
                                            if prev.get(g.game_number) else None))
            if not rows:
                continue
            lines.append(f"**#{g.game_number} {g.name}** — {g.status}")
            lines.append("")
            lines.append("| Prize | Wins left | Δ since last | Weight |")
            lines.append("|-------|----------:|:------------:|:------:|")
            for r in rows:
                rem = "—" if r["remaining"] is None else f"{r['remaining']:,}"
                if r["delta"] is None:
                    d = "—"
                elif r["delta"] == 0:
                    d = "0"
                else:
                    d = f"{'▼' if r['delta'] < 0 else '▲'}{abs(r['delta']):,}"
                lines.append(f"| {r['value']} | {rem} | {d} | ×{r['weight']} |")
            lines.append("")

    # Best games to bring in (catalog scout).
    cands = bring_in_candidates(games, inventory, min_left=bring_in_min_left,
                                per_price=bring_in_per_price)
    if cands:
        lines.append("## Best games to bring in (fresh, by price)")
        lines.append("")
        lines.append("| Price | Game | # | Win odds | Density | % left |")
        lines.append("|------:|------|---|:-------:|:-------:|:------:|")
        for price in sorted(cands, reverse=True):
            for g in cands[price]:
                jd = f"{g.jackpot_density:.2f}" if g.jackpot_density is not None else "—"
                lines.append(f"| ${price:g} | {g.name} | {g.game_number} | "
                             f"1:{g.odds_value:g} | {jd} | {min(1.0, g.sell_through_pct):.0%} |")
        lines.append("")

    missing = sorted(n for n in inventory if n not in games)
    if missing:
        lines.append(
            "> Note: these inventory game numbers weren't found on any PA page "
            f"(check the numbers): {', '.join(missing)}"
        )
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# HTML dashboard (GitHub Pages)
# --------------------------------------------------------------------------- #
_PAGE_CSS = """
:root{--bg:#0f1420;--card:#171e2e;--line:#26304a;--txt:#e8edf7;--muted:#93a0bd;
--green:#2ecc71;--orange:#f39c12;--red:#e74c3c;--blue:#4aa3ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:24px;margin:0 0 2px}.sub{color:var(--muted);margin:0 0 18px;font-size:13px}
.cards{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:12px 16px;min-width:120px;flex:1}
.card .n{font-size:26px;font-weight:700}.card .l{color:var(--muted);font-size:12px}
.card.red .n{color:var(--red)}.card.orange .n{color:var(--orange)}.card.green .n{color:var(--green)}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:12px;overflow:hidden}
th,td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--line);font-size:13.5px}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}
tr:last-child td{border-bottom:none}td.r,th.r{text-align:right}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.b-ok{background:rgba(46,204,113,.15);color:var(--green)}
.b-swap{background:rgba(243,156,18,.18);color:var(--orange)}
.b-odds{background:rgba(231,76,60,.16);color:#ff8a7a}
.b-ended{background:rgba(231,76,60,.2);color:var(--red)}
.b-keep{background:rgba(46,204,113,.18);color:var(--green);font-weight:700}
.b-send{background:rgba(231,76,60,.22);color:#ff7a6b;font-weight:700}
.odds{font-variant-numeric:tabular-nums;font-weight:600}
.pct-bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:3px}
.pct-fill{height:100%}.muted{color:var(--muted)}
h2{font-size:16px;margin:26px 0 10px}a{color:var(--blue)}
.alert{background:var(--card);border-left:3px solid var(--orange);border-radius:8px;
padding:10px 14px;margin:6px 0;font-size:14px}
.alert.crit{border-left-color:var(--red)}
footer{color:var(--muted);font-size:12px;margin-top:28px}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:6px 0;padding:6px 12px}
summary{cursor:pointer;font-size:13.5px;color:var(--txt)}
table.tiers{margin:8px 0 4px;background:transparent}
table.tiers th,table.tiers td{padding:5px 8px;font-size:13px}
.up{color:var(--green)}.dn{color:var(--orange)}
"""


def bring_in_candidates(games: dict[str, Game], inventory: set[str], *,
                        min_left: float = 0.6, per_price: int = 4) -> dict[float, list[Game]]:
    """Best FRESH games to bring in, grouped by price (active, not carried, plenty
    of prizes left), ranked by win odds then jackpot density."""
    from collections import defaultdict
    by_price: dict[float, list[Game]] = defaultdict(list)
    for num, g in games.items():
        if (g.status == "active" and num not in inventory
                and g.odds_value is not None and g.sell_through_pct is not None
                and g.sell_through_pct >= min_left):
            by_price[g.price or 0].append(g)
    out = {}
    for price, gs in by_price.items():
        gs.sort(key=lambda g: (g.odds_value, -(g.jackpot_density or 0)))
        out[price] = gs[:per_price]
    return out


def _bar_color(pct: float | None, ended: bool) -> str:
    if ended:
        return "var(--red)"
    if pct is None:
        return "var(--muted)"
    if pct < 0.40:
        return "var(--orange)"
    return "var(--green)"


def _tier_details_html(g: Game, prev: Game | None, e) -> str:
    """An expandable per-game prize-tier table: every price, wins left of total,
    % remaining (true, from the Bulletin), change since last scrape, and weight."""
    health = g.tier_health()
    if not health:
        return ""
    deltas = {r["value"]: r["delta"] for r in g.tier_table(prev_tiers=prev.prize_tiers if prev else None)}
    trs = []
    for r in health:
        rem = "—" if r["remaining"] is None else f"{r['remaining']:,}"
        tot = "" if r["original"] is None else f" / {r['original']:,}"
        pct = "—" if r["pct"] is None else f"{min(1.0, r['pct']):.0%}"
        dv = deltas.get(r["value"])
        if dv is None:
            d, cls = "—", "muted"
        elif dv == 0:
            d, cls = "0", "muted"
        elif dv < 0:
            d, cls = f"▼{abs(dv):,}", "dn"
        else:
            d, cls = f"▲{dv:,}", "up"
        trs.append(
            f"<tr><td>{e(r['value'] or '—')}</td><td class='r'>{rem}{tot}</td>"
            f"<td class='r'>{pct}</td><td class='r {cls}'>{d}</td>"
            f"<td class='r muted'>×{r['weight']}</td></tr>"
        )
    return (
        f"<details><summary>{e(g.name)} — show all prizes</summary>"
        f"<table class='tiers'><thead><tr><th>Prize</th><th class='r'>Wins left / total</th>"
        f"<th class='r'>% left</th><th class='r'>Δ last scrape</th><th class='r'>Weight</th></tr></thead>"
        f"<tbody>{''.join(trs)}</tbody></table></details>"
    )


def render_html(
    alerts: list[Alert],
    games: dict[str, Game],
    *,
    inventory: set[str],
    thresholds: Thresholds,
    captured_at: str,
    baseline: bool = False,
    previous: dict[str, Game] | None = None,
    bring_in_min_left: float = 0.6,
    bring_in_per_price: int = 4,
) -> str:
    """A self-contained dashboard page for GitHub Pages (no external assets)."""
    e = _html.escape
    prev = previous or {}
    owned = [games[n] for n in sorted(inventory) if n in games]
    recs = {g.game_number: recommendation(g, thresholds) for g in owned}
    # Send-backs first (what to act on), then by price high→low, then best odds.
    owned.sort(key=lambda g: (recs[g.game_number][0] != "send_back",
                              -(g.price or 0), g.odds_value or 99))

    n_weak = sum(1 for g in owned if thresholds.weak_odds is not None
                 and g.odds_value is not None and g.odds_value > thresholds.weak_odds)
    n_ended = sum(1 for g in owned if g.status == "ended")
    n_send = sum(1 for a, _ in recs.values() if a == "send_back")
    n_keep = len(owned) - n_send

    rows = []
    for g in owned:
        pct = g.top_prize_pct_remaining
        low = _is_low(g, thresholds)[0]
        badges = []
        if g.status == "ended":
            badges.append(f'<span class="badge b-ended">🔴 ENDED'
                          f'{" " + e(g.sales_end_date) if g.sales_end_date else ""}</span>')
        if low:
            badges.append('<span class="badge b-swap">🟠 SWAP</span>')
        if (thresholds.weak_odds is not None and g.odds_value is not None
                and g.odds_value > thresholds.weak_odds):
            badges.append('<span class="badge b-odds">🔻 WEAK ODDS</span>')
        if not badges:
            badges.append('<span class="badge b-ok">✅</span>')

        pct_txt = "—"
        bar = ""
        if pct is not None:
            pct_txt = f'{"~" if g.total_is_estimate else ""}{min(1.0, pct):.0%}'
            w = max(3, min(100, round(pct * 100)))
            bar = (f'<div class="pct-bar"><div class="pct-fill" '
                   f'style="width:{w}%;background:{_bar_color(pct, g.status=="ended")}"></div></div>')
        odds = f"1:{g.odds_value:g}" if g.odds_value is not None else "—"
        price = "—" if g.price is None else f"${g.price:g}"
        left = "—" if g.top_prizes_remaining is None else str(g.top_prizes_remaining)
        total = "" if g.top_prizes_total is None else f"/{g.top_prizes_total}"
        lower = "—" if g.lower_wins_remaining is None else f"{g.lower_wins_remaining:,}"
        jd = g.jackpot_density
        jd_s = "—" if jd is None else f"{jd:.2f}"
        jd_cls = "up" if (jd is not None and jd >= 1.15) else ("dn" if (jd is not None and jd < 0.85) else "muted")
        act, reason = recs[g.game_number]
        if act == "send_back":
            action_html = f'<span class="badge b-send" title="{e(reason)}">SEND BACK</span>'
        else:
            action_html = f'<span class="badge b-keep" title="{e(reason)}">KEEP</span>'
        moved = last_move_label(g, captured_at)
        moved_cls = "up" if (g.last_changed and _days_between(g.last_changed, captured_at) <= 1) else "muted"
        rows.append(
            f"<tr><td>{e(g.name)}</td><td class='muted'>{e(g.game_number)}</td>"
            f"<td class='r'>{price}</td><td class='muted'>{e(g.on_sale_date or '—')}</td>"
            f"<td class='odds'>{odds}</td>"
            f"<td class='r'>{left}{total} <span class='muted'>({pct_txt})</span>{bar}</td>"
            f"<td class='r'>{lower}</td>"
            f"<td class='r {jd_cls}'>{jd_s}</td>"
            f"<td class='{moved_cls}'>{e(moved)}</td>"
            f"<td>{action_html}</td></tr>"
        )

    alert_html = ""
    if alerts:
        items = "".join(
            f'<div class="alert{" crit" if a.severity == Severity.CRITICAL else ""}">'
            f'{_SEV_EMOJI[a.severity]} {e(a.message)}</div>' for a in alerts)
        alert_html = f"<h2>New alerts</h2>{items}"
    elif baseline:
        alert_html = '<div class="alert">🏁 Baseline established — tracking started, no alerts yet.</div>'

    tier_html = "".join(_tier_details_html(g, prev.get(g.game_number), e) for g in owned)

    # "Best games to bring in" board — fresh games you don't carry, ranked per price.
    cands = bring_in_candidates(games, inventory, min_left=bring_in_min_left,
                                per_price=bring_in_per_price)
    bring_rows = []
    for price in sorted(cands, reverse=True):
        for i, g in enumerate(cands[price]):
            jd = g.jackpot_density
            bring_rows.append(
                f"<tr><td class='r'>{'$%g' % price if i == 0 else ''}</td>"
                f"<td>{e(g.name)}</td><td class='muted'>{e(g.game_number)}</td>"
                f"<td class='odds'>1:{g.odds_value:g}</td>"
                f"<td class='r'>{'%.2f' % jd if jd is not None else '—'}</td>"
                f"<td class='r'>{min(1.0, g.sell_through_pct):.0%}</td></tr>"
            )
    bring_html = (
        "<h2>Best games to bring in — fresh, by price</h2>"
        "<p class='sub'>Active games you don't carry with most prizes still left, ranked by "
        "win odds (then jackpot density). When you send one back, grab its same-price replacement here.</p>"
        "<table><thead><tr><th class='r'>Price</th><th>Game</th><th>#</th>"
        "<th>Win odds</th><th class='r'>Density</th><th class='r'>% left</th></tr></thead>"
        f"<tbody>{''.join(bring_rows)}</tbody></table>"
        if bring_rows else
        "<h2>Best games to bring in</h2><p class='sub'>Catalog scan is still filling in "
        "(the full prize structure for every active game is fetched over the next few runs). "
        "Check back shortly.</p>"
    )

    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>Valley Lotto — PA Scratch-Off Tracker</title><style>{_PAGE_CSS}</style></head>
<body><div class="wrap">
<h1>🎟️ Valley Lotto</h1>
<p class="sub">PA scratch-off tracker · updated {e(captured_at)} · auto-refreshes</p>
<div class="cards">
<div class="card"><div class="n">{len(owned)}</div><div class="l">games tracked</div></div>
<div class="card green"><div class="n">{n_keep}</div><div class="l">🟢 keep</div></div>
<div class="card red"><div class="n">{n_send}</div><div class="l">🔴 send back</div></div>
<div class="card red"><div class="n">{n_ended}</div><div class="l">⛔ ended</div></div>
</div>
{alert_html}
<h2>Your games — sorted by win odds (your low-prize signal)</h2>
<table><thead><tr><th>Game</th><th>#</th><th class="r">Price</th><th>Started</th>
<th>Win odds</th><th class="r">Prizes left (est.)</th><th class="r">Lower prizes left</th><th class="r" title="top-prize % ÷ sell-through; >1 = jackpots still dense, <1 = picked over">Jackpot density</th><th>Last move</th><th>Action</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p class="sub" style="margin-top:12px"><b>Win odds (1:X) is the number that matters.</b> It's the chance a ticket wins
<i>any</i> prize — and since the cheap break-even prizes hugely outnumber the jackpots, it's effectively a
<b>low-prize-weighted</b> figure (lower = better chance to win something). <b>🔻 WEAK ODDS</b> = worse than 1:{thresholds.weak_odds:g}, a poor game to stock.
<b>Prizes left (est.)</b> = how much of the <i>whole</i> game is unsold (estimated from the top prizes — works because prizes are shuffled evenly through the pack, so it tracks the cheap prizes too).
<b>🟠 SWAP</b> = under 40% left. <b>Lower prizes left</b> = non-jackpot wins still in the pack. <b>🔴 ENDED</b> = sales stopped.<br>
<b>Jackpot density</b> = top-prize % ÷ sell-through (from PA's official prize structure). Above 1 = the big prizes are hanging around
longer than the game's sell-through predicts (good for jackpot-hunting customers); below 1 = the jackpots got claimed early (picked over).
Green ≥1.15, orange &lt;0.85.</p>
<h2>All prize tiers — cheapest weighted heaviest</h2>
<p class="sub">Every published prize per game, the wins still left, and the change since the last scrape
(▼ = claimed). Weights run heaviest at the bottom (cheapest prize). We scrape twice a day, so this trend
builds over time.</p>
{tier_html}
{bring_html}
<footer>Generated by <a href="https://github.com/pritpnp/Valley_Lotto">Valley_Lotto</a> ·
data from palottery.pa.gov · for retailer use · 30-day history in /data.</footer>
</div></body></html>"""


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
