"""Load ``config.yaml`` and normalize it."""

from __future__ import annotations

from pathlib import Path

import yaml

from .rules import Thresholds


class Config:
    def __init__(self, raw: dict):
        self.raw = raw or {}
        # Inventory: the game numbers you currently carry on the counter.
        self.inventory: set[str] = {
            str(x).strip() for x in (self.raw.get("inventory") or [])
        }
        self.thresholds = Thresholds.from_config(self.raw.get("thresholds"))
        self.report_all_games: bool = bool(self.raw.get("report_all_games", False))
        # How many days of scraped + parsed snapshots to keep (twice-daily scrapes).
        self.retention_days: int = int(self.raw.get("retention_days", 30))

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        p = Path(path)
        if not p.exists():
            return cls({})
        return cls(yaml.safe_load(p.read_text()) or {})
