"""Telegram messages: new games, ended games, and anything that needs a look.

Turned on by two settings, kept as GitHub repository secrets and never in the
code: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. Without both it does nothing.

    python -m lottery_tracker.telegram "some text"     # send a one-off message

Sending must never break the run that calls it. A failed send is reported in
the log (without the token) and the run carries on.
"""

from __future__ import annotations

import os
import sys

from .model import Game
from .rules import Alert

API = "https://api.telegram.org/bot{token}/sendMessage"
LIMIT = 4000          # Telegram's cap is 4096 characters per message

# A game is "picked over" once fewer than this share of its medium prizes are
# left (PA's reported prize levels below the top). Top prizes alone never make
# a game picked over: a game can lose its jackpot and still pay out well.
PICKED_OVER = 0.20


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _chunks(text: str) -> list[str]:
    """Split on line breaks into pieces Telegram will accept."""
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > LIMIT:                     # one enormous line
            out.append(line[:LIMIT])
            line = line[LIMIT:]
        if len(cur) + len(line) > LIMIT:
            out.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        out.append(cur)
    return out


def send(text: str) -> bool:
    """Send a message. Returns True if every part was delivered."""
    if not configured() or not text.strip():
        return False
    import requests
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    ok = True
    for part in _chunks(text):
        try:
            r = requests.post(API.format(token=token), timeout=20, json={
                "chat_id": chat, "text": part, "disable_web_page_preview": True})
            if r.status_code != 200:
                ok = False
                # Telegram's own description, never the URL (it holds the token).
                desc = ""
                try:
                    desc = r.json().get("description", "")
                except ValueError:
                    pass
                print(f"WARNING: Telegram refused the message ({r.status_code}) {desc}",
                      file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — a message must never stop the run
            ok = False
            print(f"WARNING: Telegram message not sent: {type(e).__name__}", file=sys.stderr)
    return ok


def _facts(g: Game) -> list[str]:
    """Plain facts about a game, only the ones PA's figures actually support."""
    bits = []
    if g.price is not None:
        bits.append(f"${g.price:g} ticket")
    if g.odds_value:
        bits.append(f"wins something 1 in {g.odds_value:g}")
    pair = g.top_prize_pair
    if pair and g.top_prize_value:
        bits.append(f"{pair[0]:,} of {pair[1]:,} top prizes ({g.top_prize_value}) left")
    return bits


def _low_line(g: Game, best: dict) -> str:
    """One game, in plain words: how picked over, how often it pays back more."""
    bits = [f"{g.medium_pct_remaining:.0%} of medium prizes left"]
    if g.win_back_odds:
        b = best.get(g.price)
        tail = f" (best ${g.price:g} game: 1 in {b:.1f})" if b and b < g.win_back_odds - 0.05 else ""
        bits.append(f"wins more than ${g.price:g} 1 in {g.win_back_odds:.1f}{tail}")
    pair = g.top_prize_pair
    if pair:
        bits.append(f"top prize {pair[0]:,} of {pair[1]:,} left")
    return f"• {g.name} (#{g.game_number}): " + "; ".join(bits)


def _best_win_back(games: dict[str, Game]) -> dict:
    best: dict = {}
    for g in games.values():
        if g.status == "active" and g.win_back_odds and g.price:
            best[g.price] = min(best.get(g.price, 1e9), g.win_back_odds)
    return best


def picked_over(games: dict[str, Game]) -> list[Game]:
    return sorted((g for g in games.values() if g.status == "active"
                   and g.medium_pct_remaining is not None
                   and g.medium_pct_remaining < PICKED_OVER),
                  key=lambda g: (g.price or 0, g.medium_pct_remaining))


def low_games_list(games: dict[str, Game]) -> str:
    """Every game on sale that's picked over, cheapest first."""
    low = picked_over(games)
    if not low:
        return "📉 No game on sale is picked over right now."
    best = _best_win_back(games)
    lines = [f"📉 Picked-over games on sale ({len(low)})",
             f"Under {PICKED_OVER:.0%} of their medium prizes left. Medium prizes are the "
             "levels PA reports, below the top prize.", ""]
    price = None
    for g in low:
        if g.price != price:
            price = g.price
            lines.append(f"${price:g} tickets")
        lines.append(_low_line(g, best))
    return "\n".join(lines)


def newly_picked_over(current: dict[str, Game], previous: dict[str, Game]) -> list[Game]:
    """Games that crossed into picked over since the last run (told once)."""
    out = []
    for g in picked_over(current):
        was = previous.get(g.game_number)
        if was is not None and was.medium_pct_remaining is not None \
                and was.medium_pct_remaining >= PICKED_OVER:
            out.append(g)
    return out


def game_news(alerts: list[Alert], games: dict[str, Game],
              previous: dict[str, Game] | None = None) -> str:
    """One message for the games that went on sale, ended, or became picked over."""
    new = [a for a in alerts if a.kind == "new"]
    ended = [a for a in alerts if a.kind == "ended"]
    lines: list[str] = []
    if new:
        lines.append(f"🆕 New on sale ({len(new)})")
        for a in sorted(new, key=lambda a: a.game_number):
            g = games.get(a.game_number)
            facts = _facts(g) if g else []
            lines.append(f"• #{a.game_number} {a.name}" + (" — " + ", ".join(facts) if facts else ""))
        lines.append("")
    if ended:
        lines.append(f"🏁 Sales ended ({len(ended)})")
        for a in sorted(ended, key=lambda a: a.game_number):
            g = games.get(a.game_number)
            bits = []
            if g and g.price is not None:
                bits.append(f"${g.price:g}")
            if g and g.sales_end_date:
                bits.append(f"ended {g.sales_end_date}")
            if g and g.claim_deadline:
                bits.append(f"cash winners until {g.claim_deadline}")
            lines.append(f"• #{a.game_number} {a.name}" + (" — " + ", ".join(bits) if bits else ""))
            if a.owned:
                lines.append("  ⚠️ You carry this one: pull it and settle the packs.")
        lines.append("")
    newly = newly_picked_over(games, previous) if previous else []
    if newly:
        best = _best_win_back(games)
        lines.append(f"📉 Now picked over ({len(newly)})")
        lines += [_low_line(g, best) for g in newly]
    return "\n".join(lines).strip()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--low-games"]:
        from pathlib import Path
        from .state import load_state
        state = Path(args[1]) if len(args) > 1 else Path("data/state.json")
        text = low_games_list(load_state(state))
        print(text)
    else:
        text = " ".join(args) if args else sys.stdin.read()
    if not configured():
        print("Telegram isn't set up (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID); nothing sent.")
        return 0
    return 0 if send(text) else 1


if __name__ == "__main__":
    raise SystemExit(main())
