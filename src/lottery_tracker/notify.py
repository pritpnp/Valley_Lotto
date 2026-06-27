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
from .rules import Alert, RatingWeights, Severity, Thresholds, rate, recommendation

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
    weights: RatingWeights | None = None,
    captured_at: str,
    baseline: bool = False,
    previous: dict[str, Game] | None = None,
    bring_in_min_left: float = 0.6,
    bring_in_per_price: int = 4,
) -> str:
    """Human-readable Markdown: today's alerts + a health table for your games."""
    weights = weights or RatingWeights()
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

    fresh = new_games(games, captured_at)
    if fresh:
        lines.append(f"## 🆕 New games just on sale ({len(fresh)})")
        lines.append("")
        for g in fresh:
            price = f"${g.price:g}" if g.price is not None else "—"
            odds = f"1:{g.odds_value:g}" if g.odds_value is not None else "—"
            lines.append(f"- **#{g.game_number} {g.name}** ({price}, odds {odds}, on sale {g.on_sale_date or '—'})")
        lines.append("")

    # Health table for the games you carry — sorted by overall odds (best first),
    # because the odds are the real "will a customer win anything / break even?"
    # number and they barely move over a game's life.
    owned_games = [games[n] for n in sorted(inventory) if n in games]
    recs = {g.game_number: recommendation(g, thresholds, weights) for g in owned_games}
    ratings = {g.game_number: rate(g, weights)[0] for g in owned_games}
    owned_games.sort(key=lambda g: (recs[g.game_number][0] != "send_back",
                                    ratings[g.game_number] if ratings[g.game_number] is not None else 999))
    if owned_games:
        send = [g for g in owned_games if recs[g.game_number][0] == "send_back"]
        lines.append(f"## Recommendation: send back {len(send)}, keep {len(owned_games) - len(send)}")
        lines.append("")
        if send:
            lines.append("**🔴 Send back — and what to swap in (same price):**")
            lines.append("")
            for g in send:
                swaps = swap_target(games, inventory, g.price, weights, n=2)
                swap_s = (" → swap to " + ", ".join(f"#{s.game_number} {s.name}" for s in swaps)
                          if swaps else " → no strong same-price replacement (consider dropping this price)")
                lines.append(f"- #{g.game_number} {g.name} (${g.price:g}) — {recs[g.game_number][1]}.{swap_s}")
            lines.append("")
        lines.append("| Game | # | Price | Rating | Win odds | % left (all) | Low-prize % | Density | Action |")
        lines.append("|------|---|------:|:------:|:-------:|:-----------:|:-----------:|:-------:|--------|")
        for g in owned_games:
            r = ratings[g.game_number]
            rating_s = "—" if r is None else f"{r:.0f}/100"
            pl = g.overall_pct_remaining
            pl_s = "—" if pl is None else f"{min(1.0, pl):.0%}"
            lp = g.low_prize_pct_remaining
            lp_s = "—" if lp is None else f"{min(1.0, lp):.0%}"
            price = "—" if g.price is None else f"${g.price:g}"
            odds = f"1:{g.odds_value:g}" if g.odds_value is not None else "—"
            jd = g.jackpot_density
            jd_s = "—" if jd is None else (f"{jd:.2f}" if g.jackpot_density_significant else f"{jd:.2f} (n/s)")
            act, reason = recs[g.game_number]
            action = ("🔴 **SEND BACK**" if act == "send_back" else "🟢 KEEP") + f" — {reason}"
            lines.append(
                f"| {g.name} | {g.game_number} | {price} | {rating_s} | {odds} | "
                f"{pl_s} | {lp_s} | {jd_s} | {action} |"
            )
        lines.append("")
        lines.append(
            "> **Rating (0–100)** is a weighted blend of the factors below; under "
            f"{weights.cutoff:g} → SEND BACK. Set the weights in `config.yaml`.\n"
            "> - **Win odds (1:X)** — chance a ticket wins *any* prize (the break-even signal). "
            "Lower is better.\n"
            "> - **% left (all)** — true share of the *whole* game still unsold "
            "(Σ wins remaining ÷ Σ original wins over every tracked tier — dominated by the abundant "
            "cheap prizes, not the noisy jackpot count).\n"
            "> - **Low-prize %** — share of the *cheap* prizes left (what customers actually win).\n"
            "> - **Density** — top prizes vs sell-through; shown **(n/s)** when it's small-sample "
            "noise rather than a real signal, in which case it does not affect the rating."
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
                pl = g.overall_pct_remaining
                lines.append(f"| ${price:g} | {g.name} | {g.game_number} | "
                             f"1:{g.odds_value:g} | {jd} | {min(1.0, pl):.0%} |")
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
        left = g.overall_pct_remaining
        if (g.status == "active" and num not in inventory
                and g.odds_value is not None and left is not None
                and left >= min_left):
            by_price[g.price or 0].append(g)
    out = {}
    for price, gs in by_price.items():
        gs.sort(key=lambda g: (g.odds_value, -(g.jackpot_density or 0)))
        out[price] = gs[:per_price]
    return out


def swap_target(games: dict[str, Game], inventory: set[str], price, weights: RatingWeights,
                *, n: int = 2) -> list[Game]:
    """Best same-price replacements for a game you're sending back: active, not
    carried, KEEP-worthy (rating above cutoff), highest rating first."""
    if price is None:
        return []
    cands = []
    for num, g in games.items():
        if g.status != "active" or num in inventory or g.price != price:
            continue
        score, _ = rate(g, weights)
        if score is None or score < weights.cutoff:
            continue
        cands.append((score, g))
    cands.sort(key=lambda t: t[0], reverse=True)
    return [g for _, g in cands[:n]]


def new_games(games: dict[str, Game], now_iso: str, within_days: int = 10) -> list[Game]:
    """Active games that newly appeared on the ActivePrint list (launched after we
    started tracking, within the last ``within_days``)."""
    firsts = [g.first_seen for g in games.values() if g.first_seen]
    if not firsts:
        return []
    baseline_date = min(firsts)  # the initial cohort all shares this; not "new"
    out = []
    for g in games.values():
        if g.status != "active" or not g.first_seen or g.first_seen == baseline_date:
            continue
        try:
            d = _days_between(g.first_seen, now_iso)
        except Exception:  # noqa: BLE001
            continue
        if 0 <= d <= within_days:
            out.append(g)
    out.sort(key=lambda g: (-(g.price or 0), g.odds_value or 99))
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
    weights: RatingWeights | None = None,
    captured_at: str,
    baseline: bool = False,
    previous: dict[str, Game] | None = None,
    bring_in_min_left: float = 0.6,
    bring_in_per_price: int = 4,
) -> str:
    """A self-contained dashboard page for GitHub Pages (no external assets)."""
    e = _html.escape
    weights = weights or RatingWeights()
    prev = previous or {}
    owned = [games[n] for n in sorted(inventory) if n in games]
    recs = {g.game_number: recommendation(g, thresholds, weights) for g in owned}
    ratings = {g.game_number: rate(g, weights)[0] for g in owned}
    # Send-backs first (what to act on), then worst rating first.
    owned.sort(key=lambda g: (recs[g.game_number][0] != "send_back",
                              ratings[g.game_number] if ratings[g.game_number] is not None else 999))

    n_weak = sum(1 for g in owned if thresholds.weak_odds is not None
                 and g.odds_value is not None and g.odds_value > thresholds.weak_odds)
    n_ended = sum(1 for g in owned if g.status == "ended")
    n_send = sum(1 for a, _ in recs.values() if a == "send_back")
    n_keep = len(owned) - n_send

    rows = []
    for g in owned:
        rating = ratings[g.game_number]
        # Headline "% left" is now the robust whole-game figure (Σrem/Σorig), not the
        # noisy top-prize count. Falls back to the estimate when we lack originals.
        pct = g.overall_pct_remaining
        est = pct is None
        if pct is None:
            pct = g.top_prize_pct_remaining

        odds = f"1:{g.odds_value:g}" if g.odds_value is not None else "—"
        price = "—" if g.price is None else f"${g.price:g}"

        # Rating cell with a colored bar.
        if rating is None:
            rating_html = "<span class='muted'>—</span>"
        else:
            rcol = "var(--green)" if rating >= 65 else ("var(--orange)" if rating >= weights.cutoff else "var(--red)")
            rw = max(3, min(100, round(rating)))
            rating_html = (f"<b style='color:{rcol}'>{rating:.0f}</b><span class='muted'>/100</span>"
                           f"<div class='pct-bar'><div class='pct-fill' style='width:{rw}%;background:{rcol}'></div></div>")

        # % left (all prizes) cell.
        if pct is None:
            pct_html = "<span class='muted'>—</span>"
        else:
            tot = ""
            rows_o = g._tiers_with_orig()
            if rows_o:
                tot = f" <span class='muted'>({sum(r['remaining'] for r in rows_o):,}/{sum(r['original'] for r in rows_o):,})</span>"
            w = max(3, min(100, round(pct * 100)))
            bar = (f"<div class='pct-bar'><div class='pct-fill' "
                   f"style='width:{w}%;background:{_bar_color(pct, g.status=='ended')}'></div></div>")
            pct_html = f"{'~' if est else ''}{min(1.0, pct):.0%}{tot}{bar}"

        # Low-prize % left.
        lp = g.low_prize_pct_remaining
        lp_html = "<span class='muted'>—</span>" if lp is None else f"{min(1.0, lp):.0%}"

        # Jackpot density — flagged when it's just small-sample noise.
        jd = g.jackpot_density
        if jd is None:
            jd_html = "<span class='muted'>—</span>"
        elif not g.jackpot_density_significant:
            jd_html = f"<span class='muted' title='not statistically significant — ignored in the rating'>{jd:.2f} n/s</span>"
        else:
            jd_cls = "up" if jd >= 1.15 else ("dn" if jd < 0.85 else "muted")
            jd_html = f"<span class='{jd_cls}'>{jd:.2f}</span>"

        act, reason = recs[g.game_number]
        if g.status == "ended":
            act_extra = f'<div><span class="badge b-ended">🔴 ENDED{" " + e(g.sales_end_date) if g.sales_end_date else ""}</span></div>'
        else:
            act_extra = ""
        if act == "send_back":
            swaps = swap_target(games, inventory, g.price, weights, n=2)
            if swaps:
                swap_line = ("<div class='muted' style='font-size:12px;margin-top:3px'>↳ swap to "
                             + ", ".join(f"#{e(s.game_number)} {e(s.name)}" for s in swaps) + "</div>")
            else:
                swap_line = ("<div class='muted' style='font-size:12px;margin-top:3px'>↳ no strong "
                             "same-price swap</div>")
            action_html = f'<span class="badge b-send" title="{e(reason)}">SEND BACK</span>{act_extra}{swap_line}'
        else:
            action_html = f'<span class="badge b-keep" title="{e(reason)}">KEEP</span>{act_extra}'
        rows.append(
            f"<tr><td>{e(g.name)}</td><td class='muted'>{e(g.game_number)}</td>"
            f"<td class='r'>{price}</td>"
            f"<td class='r'>{rating_html}</td>"
            f"<td class='odds'>{odds}</td>"
            f"<td class='r'>{pct_html}</td>"
            f"<td class='r'>{lp_html}</td>"
            f"<td class='r'>{jd_html}</td>"
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

    fresh = new_games(games, captured_at)
    if fresh:
        nrows = "".join(
            f"<tr><td>{e(g.name)}</td><td class='muted'>{e(g.game_number)}</td>"
            f"<td class='r'>{'$%g' % g.price if g.price is not None else '—'}</td>"
            f"<td class='odds'>{('1:%g' % g.odds_value) if g.odds_value is not None else '—'}</td>"
            f"<td class='muted'>{e(g.on_sale_date or '—')}</td></tr>" for g in fresh)
        new_html = (
            f"<h2>🆕 New games just on sale ({len(fresh)})</h2>"
            "<p class='sub'>Newly appeared on PA's active list since we started tracking — candidates to stock.</p>"
            "<table><thead><tr><th>Game</th><th>#</th><th class='r'>Price</th>"
            "<th>Win odds</th><th>On sale</th></tr></thead>"
            f"<tbody>{nrows}</tbody></table>"
        )
    else:
        new_html = ""

    tier_html = "".join(_tier_details_html(g, prev.get(g.game_number), e) for g in owned)

    # "Best games to bring in" board — fresh games you don't carry, ranked per price.
    cands = bring_in_candidates(games, inventory, min_left=bring_in_min_left,
                                per_price=bring_in_per_price)
    bring_rows = []
    for price in sorted(cands, reverse=True):
        for i, g in enumerate(cands[price]):
            jd = g.jackpot_density
            jd_txt = "—" if jd is None else (f"{jd:.2f}" if g.jackpot_density_significant else f"{jd:.2f} n/s")
            pl = g.overall_pct_remaining
            bring_rows.append(
                f"<tr><td class='r'>{'$%g' % price if i == 0 else ''}</td>"
                f"<td>{e(g.name)}</td><td class='muted'>{e(g.game_number)}</td>"
                f"<td class='odds'>1:{g.odds_value:g}</td>"
                f"<td class='r'>{jd_txt}</td>"
                f"<td class='r'>{min(1.0, pl):.0%}</td></tr>"
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
{new_html}
<h2>Your games — ranked by overall rating (act on the lowest first)</h2>
<table><thead><tr><th>Game</th><th>#</th><th class="r">Price</th>
<th class="r" title="weighted 0-100 blend of the factors below; under {weights.cutoff:g} = send back">Rating</th>
<th>Win odds</th>
<th class="r" title="true % of the whole game unsold: Σ wins remaining ÷ Σ original wins">% left (all)</th>
<th class="r" title="% of the cheap prizes left — what customers actually win">Low-prize %</th>
<th class="r" title="top-prize % ÷ sell-through; shown n/s when it's small-sample noise">Density</th>
<th>Action</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p class="sub" style="margin-top:12px"><b>Rating (0–100)</b> is a weighted blend of the factors below; under
{weights.cutoff:g} → <b>SEND BACK</b>. Hover the Action badge for the exact reason. You set the weights in
<code>config.yaml</code> (currently odds {weights.odds:g}, prizes-left {weights.prizes_left:g}, low-prize {weights.low_prize:g},
low-prize trend {weights.low_prize_skew:g}, jackpot density {weights.jackpot_density:g}).<br>
<b>Win odds (1:X)</b> — chance a ticket wins <i>any</i> prize (the break-even signal); lower is better.
<b>% left (all)</b> — the true share of the <i>whole</i> game still unsold: Σ wins remaining ÷ Σ original wins across every
tracked tier. Because the cheap prizes number in the thousands and the jackpots in single digits, this is driven by reliable
data, <i>not</i> the noisy top-prize count.
<b>Low-prize %</b> — share of the cheap prizes left (no incentive to play once these are gone).
<b>Density</b> — top prizes vs sell-through; marked <b>n/s</b> (and ignored by the rating) when it's small-sample noise
rather than a real signal. Green ≥1.15, orange &lt;0.85.</p>
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
