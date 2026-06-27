"""Persist game snapshots so we can detect *changes* between runs.

We keep a single ``state.json`` (the most recent snapshot) plus a dated copy in
``data/history/`` for an audit trail you can diff over time. Alerts are driven by
comparing the previous ``state.json`` to the freshly scraped data, so you're only
told about *transitions* (a game just ended, prizes just crossed below the line)
rather than the same status every single day.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from pathlib import Path

from .model import Game


def content_hash(games: dict[str, Game]) -> str:
    """Stable fingerprint of the *data we care about* (ignores the timestamp).

    Two scrapes hash the same only if every game's status and every prize tier's
    remaining count are identical — so a difference of even one number changes it.
    """
    canon = {
        num: {
            "status": g.status,
            "on_sale_date": g.on_sale_date,
            "sales_end_date": g.sales_end_date,
            "tiers": [[t.get("value"), t.get("remaining")] for t in (g.prize_tiers or [])],
        }
        for num, g in games.items()
    }
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode()).hexdigest()


def append_scrape_log(path: str | Path, entry: dict) -> None:
    """Append-only archive of every scrape (kept forever — never pruned)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def slugify(captured_at: str) -> str:
    """'2026-06-27T16:00:00Z' -> '2026-06-27_1600' (sortable, one per scrape)."""
    date, _, rest = captured_at.partition("T")
    hhmm = (rest[:5] or "0000").replace(":", "")
    return f"{date}_{hhmm}"


def save_snapshot(snapshots_dir: str | Path, slug: str, games: dict[str, Game],
                  *, captured_at: str) -> Path:
    """Write one parsed snapshot per scrape (kept for the retention window)."""
    d = Path(snapshots_dir)
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{slug}.json"
    out.write_text(json.dumps(
        {"captured_at": captured_at, "games": {n: g.to_dict() for n, g in games.items()}},
        sort_keys=True,
    ))
    return out


def save_raw_html(raw_dir: str | Path, slug: str, html_by_name: dict[str, str]) -> Path:
    """Store the scraped HTML (gzipped) so we keep the raw source, not just parses."""
    d = Path(raw_dir) / slug
    d.mkdir(parents=True, exist_ok=True)
    for name, html in html_by_name.items():
        with gzip.open(d / f"{name}.html.gz", "wt", encoding="utf-8") as fh:
            fh.write(html)
    return d


def prune_keep_newest(dir_path: str | Path, keep: int) -> list[str]:
    """Keep only the newest ``keep`` entries (by sortable name); delete the rest.

    Works for both the snapshot files and the raw-HTML subdirectories. Returns the
    names removed. Filenames are timestamp-prefixed, so lexical sort == time order.
    """
    d = Path(dir_path)
    if not d.exists():
        return []
    entries = sorted(p for p in d.iterdir() if p.name not in (".gitkeep",))
    removed = []
    for p in entries[:-keep] if keep > 0 else entries:
        removed.append(p.name)
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    return removed


def load_state(path: str | Path) -> dict[str, Game]:
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text() or "{}")
    games = raw.get("games", raw)  # tolerate either {"games": {...}} or a bare map
    return {num: Game.from_dict(d) for num, d in games.items()}


def save_state(path: str | Path, games: dict[str, Game], *, captured_at: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "captured_at": captured_at,
        "games": {num: g.to_dict() for num, g in games.items()},
    }
    p.write_text(json.dumps(payload, indent=2, sort_keys=True))


def load_originals(path: str | Path) -> dict[str, dict]:
    """Cache of per-game original prize counts/odds scraped from detail pages.

    Originals never change once a game is printed, so we fetch each game's detail
    page only once and reuse the cached value forever after.
    """
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text() or "{}")


def save_originals(path: str | Path, originals: dict[str, dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(originals, indent=2, sort_keys=True))


def save_history(history_dir: str | Path, games: dict[str, Game], *, captured_at: str) -> Path:
    """Write a dated snapshot. ``captured_at`` should be a date/time string."""
    d = Path(history_dir)
    d.mkdir(parents=True, exist_ok=True)
    # captured_at like "2026-06-26T13:00:00Z" -> "2026-06-26.json"
    stamp = captured_at.split("T")[0]
    out = d / f"{stamp}.json"
    out.write_text(
        json.dumps(
            {"captured_at": captured_at, "games": {n: g.to_dict() for n, g in games.items()}},
            indent=2,
            sort_keys=True,
        )
    )
    return out
