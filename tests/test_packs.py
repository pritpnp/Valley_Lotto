"""Tests for pack-size defaults, scan-based correction, and rollover bridging."""

from lottery_tracker.packs import (
    pack_size_for, observed_max_ticket, DEFAULT_PACK_SIZE_BY_PRICE, PackSizeInfo,
    PackSizeResolver,
)
from lottery_tracker.barcode import parse_ticket
from lottery_tracker.scans import Scan, compute_sold
from lottery_tracker.config import Config


def _scan(pack, ticket):
    return Scan.from_ticket(parse_ticket(f"1750-{pack}-{ticket:03d}"),
                            scanned_at="2026-08-07T08:00:00Z")


def test_price_defaults_match_research():
    # spot-check the high-confidence ones
    assert DEFAULT_PACK_SIZE_BY_PRICE[5.0].size == 60
    assert DEFAULT_PACK_SIZE_BY_PRICE[10.0].size == 60
    assert DEFAULT_PACK_SIZE_BY_PRICE[20.0].size == 30
    assert DEFAULT_PACK_SIZE_BY_PRICE[2.0].size == 150
    # $30 is a low-confidence placeholder
    assert DEFAULT_PACK_SIZE_BY_PRICE[30.0].confidence == "low"


def test_pack_size_for_accepts_various_price_forms():
    for p in (30, 30.0, "30", "$30"):
        assert pack_size_for(p).size == 20


def test_observed_scan_corrects_a_too_small_default_up():
    # $30 default is 20, but a clerk scanned ticket 028 -> pack must hold >= 29
    info = pack_size_for(30.0, observed_max=28)
    assert info.size == 29
    assert info.confidence == "observed"


def test_observed_below_default_keeps_default():
    # seeing ticket 010 doesn't shrink the $30 default of 20
    info = pack_size_for(30.0, observed_max=10)
    assert info.size == 20
    assert info.confidence == "low"


def test_override_always_wins():
    info = pack_size_for(30.0, observed_max=28, override=25)
    assert info.size == 25
    assert info.confidence == "override"


def test_unknown_price_needs_data():
    info = pack_size_for(7.0)
    assert info.size is None and info.confidence == "unknown"


def test_observed_max_ticket_helper():
    assert observed_max_ticket([10, 25, 3]) == 25
    assert observed_max_ticket([]) is None


def test_rollover_bridge_with_pack_size():
    # $30 pack of 20 (tickets 000..019). Open at 015, close on the NEXT pack at 004.
    # Old pack sold 015..019 = 5, new pack sold 000..003 = 4  -> 9 tickets, $270.
    o = _scan("0091798", 15)
    c = _scan("0091799", 4)
    res = compute_sold(o, c, price=30.0, pack_size=20)
    assert res.same_pack is False
    assert res.tickets_sold == 9
    assert res.revenue == 270.0
    assert "changeover" in res.note


def test_rollover_without_pack_size_still_flags():
    o = _scan("0091798", 15)
    c = _scan("0091799", 4)
    res = compute_sold(o, c, price=30.0)  # no pack_size
    assert res.tickets_sold is None
    assert "pack changed" in res.note


# --- config-driven resolver -------------------------------------------------

def test_resolver_by_price_config_overrides_builtin():
    r = PackSizeResolver(by_price={30: 30})   # user says $30 packs are 30, not the 20 guess
    info = r.size_for(30.0)
    assert info.size == 30
    assert info.confidence == "config-price"


def test_resolver_by_game_is_authoritative():
    r = PackSizeResolver(by_price={30: 30}, by_game={"1750": 40})
    info = r.size_for(30.0, game_number="1750")
    assert info.size == 40
    assert info.confidence == "config-game"


def test_resolver_scan_corrects_impossible_config_and_warns():
    r = PackSizeResolver(by_game={"1750": 20})
    info = r.size_for(30.0, game_number="1750", observed_max=28)  # can't fit in 20
    assert info.size == 29
    assert info.confidence == "observed"
    assert "check config" in info.source


def test_resolver_falls_back_to_builtin_when_enabled_and_unknown_when_not():
    assert PackSizeResolver().size_for(5.0).size == 60           # built-in fallback
    off = PackSizeResolver(use_builtin_fallback=False).size_for(5.0)
    assert off.size is None and off.confidence == "unknown"


def test_config_yaml_pack_sizes_load_and_resolve():
    cfg = Config.load("config.yaml")
    r = cfg.pack_resolver()
    assert r.size_for(5.0).size == 60
    assert r.size_for(20.0).size == 30
    # the $30 row is present but flagged unverified in the file
    assert r.size_for(30.0).size == 20
