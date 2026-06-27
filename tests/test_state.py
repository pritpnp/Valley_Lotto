import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lottery_tracker.model import Game  # noqa: E402
from lottery_tracker.state import content_hash, append_scrape_log, slugify  # noqa: E402


def _game(num, tiers, status="active"):
    return Game(game_number=num, status=status,
                prize_tiers=[{"value": v, "remaining": r} for v, r in tiers])


def test_hash_identical_when_data_same():
    a = {"1": _game("1", [("$100", 5), ("$10", 999)])}
    b = {"1": _game("1", [("$100", 5), ("$10", 999)])}
    assert content_hash(a) == content_hash(b)


def test_hash_changes_on_one_number():
    a = {"1": _game("1", [("$100", 5), ("$10", 999)])}
    b = {"1": _game("1", [("$100", 5), ("$10", 998)])}  # one tier down by 1
    assert content_hash(a) != content_hash(b)


def test_hash_changes_on_status():
    a = {"1": _game("1", [("$100", 5)], status="active")}
    b = {"1": _game("1", [("$100", 5)], status="ended")}
    assert content_hash(a) != content_hash(b)


def test_hash_ignores_timestamp_only_fields():
    # captured_at / odds / detail_id aren't part of the fingerprint.
    a = {"1": _game("1", [("$100", 5)])}
    b = {"1": _game("1", [("$100", 5)])}
    b["1"].odds = "1:3.99"
    b["1"].detail_id = "9999"
    assert content_hash(a) == content_hash(b)


def test_slugify():
    assert slugify("2026-06-27T16:00:00Z") == "2026-06-27_1600"


def test_scrape_log_appends(tmp_path):
    p = tmp_path / "scrape_log.jsonl"
    append_scrape_log(p, {"slug": "a", "changed": True})
    append_scrape_log(p, {"slug": "b", "changed": False})
    lines = p.read_text().splitlines()
    assert len(lines) == 2 and '"changed": false' in lines[1]
