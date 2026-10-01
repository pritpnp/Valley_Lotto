"""PA's own game pages are kept, and re-read whenever the reader improves."""

from lottery_tracker import cli, parse
from lottery_tracker.model import Game
from lottery_tracker.state import load_game_page, save_game_page


def _game():
    return {"9001": Game(game_number="9001", price=5, status="active", detail_id="77")}


def test_pages_are_kept_and_read_back(tmp_path):
    save_game_page(tmp_path, "9001", "bulletin", "<html>prizes</html>")
    assert load_game_page(tmp_path, "9001", "bulletin") == "<html>prizes</html>"
    assert load_game_page(tmp_path, "9001", "detail") is None


def _wire(monkeypatch, tmp_path, fetched):
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(parse, "parse_detail", lambda h: {"bulletin_url": "http://b", "odds": "1:4"})
    monkeypatch.setattr(parse, "parse_bulletin", lambda h: {"prize_originals": {"5.0": len(h)}})
    def fake_fetch(url):
        fetched.append(url)
        return "<detail>" if "b" not in url.split("//")[-1][:1] else "<bulletin-page>"
    monkeypatch.setattr(cli.fetch, "fetch", fake_fetch)
    monkeypatch.setattr(cli.time if hasattr(cli, "time") else __import__("time"), "sleep", lambda s: None)


def test_a_better_reader_rereads_saved_pages_without_fetching(tmp_path, monkeypatch):
    fetched = []
    _wire(monkeypatch, tmp_path, fetched)
    save_game_page(tmp_path / "pa_pages", "9001", "detail", "<d>")
    save_game_page(tmp_path / "pa_pages", "9001", "bulletin", "<saved bulletin>")
    originals = {"9001": {"prize_originals": {"5.0": 1}, "parser_version": parse.PARSER_VERSION - 1}}
    cli._enrich_with_originals(_game(), {"9001"}, originals, offline=False, save_html=False)
    assert fetched == []                                   # nothing fetched
    assert originals["9001"]["prize_originals"] == {"5.0": len("<saved bulletin>")}
    assert originals["9001"]["parser_version"] == parse.PARSER_VERSION


def test_a_failed_bulletin_fetch_never_erases_what_was_read(tmp_path, monkeypatch):
    fetched = []
    _wire(monkeypatch, tmp_path, fetched)
    monkeypatch.setattr(cli.fetch, "fetch", lambda url: (_ for _ in ()).throw(OSError("PA down"))
                        if "b" in url.split("//")[-1][:1] else "<detail>")
    originals = {"9001": {"prize_originals": {"5.0": 123}}}
    cli._enrich_with_originals(_game(), {"9001"}, originals, offline=False, save_html=False)
    assert originals["9001"]["prize_originals"] == {"5.0": 123}
