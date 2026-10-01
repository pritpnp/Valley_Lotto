"""Run every PA game, across the saved history, through all of the code.

    python tools/audit_all.py                 # last 100 days, plus the app's pages
    python tools/audit_all.py --days 30 --no-app

Two kinds of finding, kept apart on purpose:

* CODE problems: anything that crashes, or a score that isn't a number from 0
  to 100. These are bugs. The fix is a rule in the code, never a patch for one
  game.
* DATA problems: PA's numbers contradicting each other (more prizes left than
  were printed, odds that don't match the prize table, and so on). The code has
  to cope with these by itself, by leaving the inconsistent part out of the
  score, so they're counted here to show how often that happens.

The report is deterministic: the same inputs give byte-for-byte the same
output. Its fingerprint is printed at the end, so running it several times
shows whether anything varies between runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import traceback
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from lottery_tracker.model import Game  # noqa: E402
from lottery_tracker.rules import RatingWeights, Thresholds, evaluate, rate, recommendation  # noqa: E402
from lottery_tracker.notify import render_report  # noqa: E402
from lottery_app import pa_data  # noqa: E402

EXAMPLES = 5      # examples kept per finding


class Findings:
    def __init__(self):
        self.count = defaultdict(int)
        self.examples = defaultdict(list)

    def add(self, kind: str, example) -> None:
        self.count[kind] += 1
        if len(self.examples[kind]) < EXAMPLES and example not in self.examples[kind]:
            self.examples[kind].append(example)

    def to_dict(self) -> dict:
        return {k: {"count": self.count[k], "examples": self.examples[k]}
                for k in sorted(self.count)}


def _crash(f: Findings, where: str, game: str | None, e: Exception) -> None:
    tb = traceback.extract_tb(e.__traceback__)[-1]
    sig = f"{where}: {type(e).__name__} at {Path(tb.filename).name}:{tb.lineno}"
    f.add("CODE crash — " + sig, {"game": game, "error": str(e)[:160]})


def _bad_number(x) -> bool:
    return x is not None and (not isinstance(x, (int, float)) or math.isnan(x)
                              or math.isinf(x) or x < 0 or x > 100)


def _money(v) -> float | None:
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


# --- the history -------------------------------------------------------------

def history(days: int, ref: str) -> list[tuple[str, dict]]:
    """Every committed version of data/state.json in the window, oldest first.

    The window ends at the newest version, not at today's date, so running this
    again tomorrow covers exactly the same data.
    """
    log = subprocess.run(
        ["git", "log", "--format=%H %cI", "--reverse", ref, "--", "data/state.json"],
        cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n")
    entries = [line.split(" ", 1) for line in log if line]
    if not entries:
        return []
    since = datetime.fromisoformat(entries[-1][1]) - timedelta(days=days)
    out = []
    for sha, when in entries:
        if datetime.fromisoformat(when) < since:
            continue
        raw = subprocess.run(["git", "show", f"{sha}:data/state.json"], cwd=ROOT,
                             capture_output=True, text=True).stdout
        try:
            out.append((when[:16], json.loads(raw or "{}")))
        except ValueError:
            out.append((when[:16], None))
    return out


# --- per-version checks --------------------------------------------------------

def check_version(stamp: str, raw: dict | None, prev: dict[str, Game] | None,
                  f: Findings) -> dict[str, Game]:
    if raw is None:
        f.add("DATA state file unreadable", stamp)
        return prev or {}
    games: dict[str, Game] = {}
    for num, d in (raw.get("games", raw) or {}).items():
        try:
            games[num] = Game.from_dict(d)
        except Exception as e:  # noqa: BLE001
            _crash(f, "load", num, e)

    th, w = Thresholds(), RatingWeights()
    for num in sorted(games):
        g = games[num]
        tag = f"{stamp} #{num}"
        try:
            rating, factors = rate(g, w)
            if _bad_number(rating):
                f.add("CODE rating not a number from 0 to 100", {"at": tag, "rating": rating})
            for fa in factors:
                if _bad_number(fa.score):
                    f.add("CODE factor score not a number from 0 to 100",
                          {"at": tag, "factor": fa.key, "score": fa.score})
            recommendation(g, th, w)
        except Exception as e:  # noqa: BLE001
            _crash(f, "rate", tag, e)
        data_checks(g, tag, f)
        rule_checks(g, tag, f)
        if prev and num in prev:
            time_checks(prev[num], g, tag, f)

    active = {n for n, g in games.items() if g.status == "active"}
    cat = pa_data.Catalog(games=games, captured_at=raw.get("captured_at"))
    try:
        rows = pa_data.store_rows(cat, active, th, w)
        swap_checks(rows, stamp, f)
        pa_data.catalog_rankings(cat, th, w, inventory=active)
        pa_data.bring_in_candidates(cat, set(), th, weights=w)
        pa_data.new_games(cat, within_days=14, weights=w)
    except Exception as e:  # noqa: BLE001
        _crash(f, "dashboard", stamp, e)
    if prev is not None:
        try:
            alerts = evaluate(games, prev, inventory=active, thresholds=th, report_all_games=True)
            render_report(alerts, games, inventory=active, thresholds=th, weights=w,
                          captured_at=stamp, previous=prev)
        except Exception as e:  # noqa: BLE001
            _crash(f, "tracker", stamp, e)
    return games


def data_checks(g: Game, tag: str, f: Findings) -> None:
    """PA's numbers checked against each other."""
    printed = {}
    for k, v in (g.tier_originals or {}).items():
        printed[_money(k)] = v
    listed = []
    for t in g.prize_tiers or []:
        v = _money(t.get("value"))
        if v is None:
            f.add("DATA listed prize isn't a plain dollar amount", {"at": tag, "value": t.get("value")})
            continue
        listed.append(v)
        if printed and v not in printed:
            f.add("DATA listed prize level has no printed count", {"at": tag, "value": t.get("value")})
        elif printed and t.get("remaining") is not None and t["remaining"] > printed[v]:
            f.add("DATA more left than were printed",
                  {"at": tag, "value": t.get("value"), "left": t["remaining"], "printed": printed[v]})
    if listed and printed and g.top_prizes_total is not None:
        top = max(listed)
        if printed.get(top) is not None and printed[top] != g.top_prizes_total:
            f.add("DATA top-prize count belongs to a different prize",
                  {"at": tag, "top listed": top, "printed": printed[top], "used": g.top_prizes_total})
    if g.status == "active" and not g.odds_value:
        f.add("DATA active game without odds", tag)
    if printed and g.tickets_printed and g.odds_value:
        implied = g.tickets_printed / sum(printed.values())
        if abs(implied - g.odds_value) / g.odds_value > 0.05:
            f.add("DATA odds don't match the printed prize table",
                  {"at": tag, "odds": g.odds_value, "table implies": round(implied, 2)})
    if printed and g.tickets_printed and g.payout_pct and g.price:
        comp = 100 * sum(v * c for v, c in printed.items() if v) / (g.tickets_printed * g.price)
        if abs(comp - g.payout_pct) > 2:
            f.add("DATA stated payback doesn't match the printed prize table",
                  {"at": tag, "stated": g.payout_pct, "table gives": round(comp, 1)})


TABLE_FACTORS = ("prizes_left", "low_prize", "low_prize_skew", "jackpot_density")


def rule_checks(g: Game, tag: str, f: Findings) -> None:
    """Did the code obey its own rules on this game? A failure here is a bug."""
    try:
        trusted = g.printed_table_trusted
        _, factors = rate(g)
        if not trusted:
            used = [fa.key for fa in factors if fa.key in TABLE_FACTORS and fa.score is not None]
            if used:
                f.add("CODE score used a prize table that contradicts PA", {"at": tag, "factors": used})
        pair = g.top_prize_pair
        if pair is not None:
            listed = [v for t in g.prize_tiers or [] for v in [_money(t.get("value"))] if v is not None]
            printed = (g.tier_originals or {}).get(str(max(listed)))
            if printed != pair[1]:
                f.add("CODE top-prize count not matched to the same prize",
                      {"at": tag, "pair": pair, "printed for that prize": printed})
            if not trusted:
                f.add("CODE top-prize figure from an untrusted table", tag)
        pct = g.top_prize_pct_remaining
        if _bad_number(None if pct is None else 100 * pct):
            f.add("CODE top-prize share not a number from 0 to 100%", {"at": tag, "pct": pct})
    except Exception as e:  # noqa: BLE001
        _crash(f, "rules", tag, e)


def time_checks(was: Game, now: Game, tag: str, f: Findings) -> None:
    before = {t.get("value"): t.get("remaining") for t in was.prize_tiers or []}
    for t in now.prize_tiers or []:
        b = before.get(t.get("value"))
        if b is not None and t.get("remaining") is not None and t["remaining"] > b:
            f.add("DATA prizes left went UP since the previous run",
                  {"at": tag, "value": t.get("value"), "was": b, "now": t["remaining"]})


def swap_checks(rows: list[dict], stamp: str, f: Findings) -> None:
    seen = {}
    for r in rows:
        for s in r.get("swap_to") or []:
            if s["game_number"] in seen:
                f.add("CODE same replacement offered twice",
                      {"at": stamp, "game": s["game_number"]})
            seen[s["game_number"]] = r["game_number"]
        if len(r.get("swap_to") or []) > 1:
            f.add("CODE more than one replacement offered", {"at": stamp, "game": r["game_number"]})


# --- the app itself, on the current data -----------------------------------------

def check_app(f: Findings) -> int:
    from sqlalchemy import select
    from lottery_tracker.web.app import create_app
    from lottery_tracker.web.models import InventoryRow
    tmp = tempfile.mkdtemp()
    app = create_app({"DATABASE_URL": f"sqlite:///{tmp}/a.db", "SECRET_KEY": "k",
                      "DEFAULT_STORE": "audit", "SLOTS": "48", "REGISTER_CODE": None})
    app.config.update(TESTING=True)
    c = app.test_client()
    c.post("/register", data={"username": "audit", "password": "pw"})
    cat = pa_data.load_catalog(ROOT / "data" / "state.json")
    with app.config["SESSION_FACTORY"]() as db:     # carry every game on sale
        for n, g in cat.games.items():
            if g.status == "active":
                db.add(InventoryRow(store="audit", game_number=n))
        db.commit()
    pages = ["/dashboard", "/catalog", "/inventory", "/history?tab=trends"]
    pages += [f"/catalog/{n}" for n in sorted(cat.games)]
    pages += [f"/inventory/box/{b}" for b in range(1, 49)]
    for p in pages:
        try:
            r = c.get(p)
            if r.status_code >= 500:
                f.add("CODE app page failed", {"page": p, "status": r.status_code})
            elif r.status_code != 200:
                f.add("CODE app page didn't open (redirect or refusal)",
                      {"page": p, "status": r.status_code})
        except Exception as e:  # noqa: BLE001
            _crash(f, "app " + p, None, e)
    return len(pages)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=100)
    ap.add_argument("--no-app", action="store_true")
    ap.add_argument("--ref", default="origin/main", help="git history to read")
    ap.add_argument("--with-local", action="store_true",
                    help="also check data/state.json in this checkout, after the history")
    ap.add_argument("--out", default=None, help="write the full report as JSON here")
    a = ap.parse_args()

    f = Findings()
    versions = history(a.days, a.ref)
    if a.with_local:
        # This checkout's own data (e.g. just re-read with a new reader) last.
        versions.append(("current checkout", json.loads((ROOT / "data" / "state.json").read_text())))
    prev = None
    games_seen = set()
    for stamp, raw in versions:
        prev = check_version(stamp, raw, prev, f)
        games_seen |= set(prev)
    pages = 0 if a.no_app else check_app(f)

    report = {
        "versions checked": len(versions),
        "from": versions[0][0] if versions else None,
        "to": versions[-1][0] if versions else None,
        "distinct games": len(games_seen),
        "app pages opened": pages,
        "findings": f.to_dict(),
    }
    text = json.dumps(report, indent=1, sort_keys=True, default=str)
    if a.out:
        Path(a.out).write_text(text)
    code = {k: v["count"] for k, v in report["findings"].items() if k.startswith("CODE")}
    data = {k: v["count"] for k, v in report["findings"].items() if k.startswith("DATA")}
    print(f"{len(versions)} versions ({report['from']} .. {report['to']}), "
          f"{len(games_seen)} games, {pages} app pages")
    print("CODE problems:", sum(code.values()))
    for k, v in code.items():
        print(f"  {v:7}  {k}")
    print("DATA problems:", sum(data.values()))
    for k, v in data.items():
        print(f"  {v:7}  {k}")
    print("fingerprint:", hashlib.sha256(text.encode()).hexdigest()[:16])


if __name__ == "__main__":
    main()
