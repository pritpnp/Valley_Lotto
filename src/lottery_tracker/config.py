"""Load ``config.yaml`` and normalize it."""

from __future__ import annotations

from pathlib import Path

import yaml

from .packs import PackSizeResolver
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

        # Pack (book) sizes — how many tickets are in a full pack. Optional.
        # Shape in config.yaml:
        #   pack_sizes:
        #     by_price: {1: 300, 5: 60, ...}   # tickets per pack, by ticket price
        #     by_game:  {"1750": 20}            # override one game exactly
        #     use_builtin_fallback: true        # use the built-in guesses when unset
        pack_cfg = self.raw.get("pack_sizes") or {}
        self.pack_sizes_by_price: dict = pack_cfg.get("by_price") or {}
        self.pack_sizes_by_game: dict = pack_cfg.get("by_game") or {}
        self.pack_use_builtin_fallback: bool = bool(
            pack_cfg.get("use_builtin_fallback", True)
        )

    def pack_resolver(self) -> PackSizeResolver:
        """A resolver that answers pack-size questions using this config first."""
        return PackSizeResolver(
            by_price=self.pack_sizes_by_price,
            by_game=self.pack_sizes_by_game,
            use_builtin_fallback=self.pack_use_builtin_fallback,
        )

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        p = Path(path)
        if not p.exists():
            return cls({})
        return cls(yaml.safe_load(p.read_text()) or {})
