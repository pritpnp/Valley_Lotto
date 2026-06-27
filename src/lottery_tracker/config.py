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
        # Catalog scout: pull the full prize structure for EVERY active PA game
        # (not just inventory) so we can rank the best games to bring in.
        self.scout_catalog: bool = bool(self.raw.get("scout_catalog", True))
        self.bring_in_min_left: float = float(self.raw.get("bring_in_min_left", 0.6))
        self.bring_in_per_price: int = int(self.raw.get("bring_in_per_price", 4))
        # Cap how many NEW games we fetch detail+bulletin for per run, so the first
        # catalog fill is spread across a few runs instead of one huge burst.
        self.max_new_fetches: int = int(self.raw.get("max_new_fetches", 250))

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        p = Path(path)
        if not p.exists():
            return cls({})
        return cls(yaml.safe_load(p.read_text()) or {})
