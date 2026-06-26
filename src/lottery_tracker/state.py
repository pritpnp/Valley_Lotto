"""Persist game snapshots so we can detect *changes* between runs.

We keep a single ``state.json`` (the most recent snapshot) plus a dated copy in
``data/history/`` for an audit trail you can diff over time. Alerts are driven by
comparing the previous ``state.json`` to the freshly scraped data, so you're only
told about *transitions* (a game just ended, prizes just crossed below the line)
rather than the same status every single day.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import Game


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
