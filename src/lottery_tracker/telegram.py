"""Telegram messages: new games, ended games, and anything that needs a look.

Turned on by two settings, kept as GitHub repository secrets and never in the
code: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. Without both it does nothing.

    python -m lottery_tracker.telegram "some text"     # send a one-off message

Sending must never break the run that calls it. A failed send is reported in
the log (without the token) and the run carries on.
"""

from __future__ import annotations

import html
import os
import sys

from .model import Game
from .rules import Alert, RatingWeights, money, rate

API = "https://api.telegram.org/bot{token}/sendMessage"
LIMIT = 4000          # Telegram's cap is 4096 characters per message

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


def send(text: str, *, formatted: bool = False) -> bool:
    """Send a message. Returns True if every part was delivered.

    formatted: the text uses Telegram's HTML tags (<b>, <u>), and any text from
    PA inside it has been escaped.
    """
    if not configured() or not text.strip():
        return False
    import requests
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    ok = True
    for part in _chunks(text):
        try:
            body = {"chat_id": chat, "text": part, "disable_web_page_preview": True}
            if formatted:
                body["parse_mode"] = "HTML"
            r = requests.post(API.format(token=token), timeout=20, json=body)
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


def _rule(w: RatingWeights) -> str:
    return (f"<i>Sells under {w.cutoff:g}% of the typical game at its price, "
            f"across PA</i>")


def _score(g: Game, w: RatingWeights):
    return rate(g, w)[0]


def send_back(games: dict[str, Game], w: RatingWeights | None = None) -> list[Game]:
    """Games on sale whose rating is under the cutoff, cheapest price first and
    worst first within it. The same rating the app shows."""
    w = w or RatingWeights()
    low = [(g, _score(g, w)) for g in games.values() if g.status == "active"]
    low = [(g, s) for g, s in low if s is not None and s < w.cutoff]
    return [g for g, s in sorted(low, key=lambda gs: (gs[0].price or 0, gs[1]))]


def _sells(g: Game) -> str:
    return f" · {money(g.sales_per_day)}/day" if g.sales_per_day is not None else ""


def _name(g_or_alert) -> str:
    """A game's name and number, bold. PA's text is escaped for Telegram."""
    return f"<b>{html.escape(g_or_alert.name)} #{g_or_alert.game_number}</b>"


def _price(price) -> str:
    return f"<u>${price:g}</u>" if price is not None else ""


def low_games_list(games: dict[str, Game], w: RatingWeights | None = None) -> str:
    """Every game on sale to send back, by price. Formatted (HTML)."""
    w = w or RatingWeights()
    low = send_back(games, w)
    if not low:
        return "📉 No game on sale is below the line right now."
    lines = [f"📉 Send back ({len(low)})", _rule(w)]
    price = object()
    for g in low:
        if g.price != price:
            price = g.price
            lines += ["", _price(price)]
        lines.append(_name(g) + _sells(g))
    return "\n".join(lines)


def newly_send_back(current: dict[str, Game], previous: dict[str, Game],
                    w: RatingWeights | None = None) -> list[Game]:
    """Games that dropped under the line since the last run (told once)."""
    w = w or RatingWeights()
    out = []
    for g in send_back(current, w):
        was = previous.get(g.game_number)
        before = _score(was, w) if was is not None else None
        if before is not None and before >= w.cutoff:
            out.append(g)
    return out


def game_news(alerts: list[Alert], games: dict[str, Game],
              previous: dict[str, Game] | None = None, w: RatingWeights | None = None) -> str:
    """One message for the games that went on sale, ended, or dropped under the line.
    Formatted (HTML): prices underlined, games bold."""
    new = [a for a in alerts if a.kind == "new"]
    ended = [a for a in alerts if a.kind == "ended"]
    lines: list[str] = []
    if new:
        lines.append(f"🆕 New on sale ({len(new)})")
        for a in sorted(new, key=lambda a: a.game_number):
            g = games.get(a.game_number)
            lines.append(f"{_price(g.price if g else None)} {_name(a)}".strip())
        lines.append("")
    if ended:
        lines.append(f"🏁 Sales ended ({len(ended)})")
        for a in sorted(ended, key=lambda a: a.game_number):
            g = games.get(a.game_number)
            when = f" (cash winners until {html.escape(g.claim_deadline)})" if g and g.claim_deadline else ""
            lines.append(f"{_price(g.price if g else None)} {_name(a)}{when}".strip())
            if a.owned:
                lines.append("  ⚠️ You carry this one: pull it and settle the packs.")
        lines.append("")
    w = w or RatingWeights()
    newly = newly_send_back(games, previous, w) if previous else []
    if newly:
        lines += [f"📉 Now send back ({len(newly)})", _rule(w)]
        lines += [f"{_price(g.price)} {_name(g)}{_sells(g)}" for g in newly]
    return "\n".join(lines).strip()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--low-games"]:
        from pathlib import Path
        from .config import Config
        from .state import load_state
        state = Path(args[1]) if len(args) > 1 else Path("data/state.json")
        text = low_games_list(load_state(state), Config.load("config.yaml").rating_weights)
        print(text)
        formatted = True
    else:
        text = " ".join(args) if args else sys.stdin.read()
        formatted = False
    if not configured():
        print("Telegram isn't set up (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID); nothing sent.")
        return 0
    return 0 if send(text, formatted=formatted) else 1


if __name__ == "__main__":
    raise SystemExit(main())
