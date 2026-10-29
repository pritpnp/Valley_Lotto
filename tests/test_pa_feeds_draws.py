"""PA's number feed and the old scraper's lottery.json, read exactly or not at all.

Uses the real files fetched on 2026-10-28 (tests/fixtures/pa/).
"""

from pathlib import Path

import pytest

from lottery_tracker.pa_feeds import draws
from lottery_tracker.pa_feeds.draws import Draw, FeedError

PA = Path(__file__).parent / "fixtures" / "pa"
FEED = (PA / "game-number-feed_2026-10-28.xml").read_bytes()
OLD = (PA / "old-scraper-lottery_2026-10-28.json").read_text()

WB7 = [{"code": "WB", "label": "Wild Ball", "value": "7"}]

# Every game in the real feed, as it must be read:
# id, title, draw date, numbers, extras, next draw, jackpot text, amount, cash text, amount
EXPECTED = [
    ("31", "PICK 2 (Day)", "2026-10-28", ["7", "3"], WB7, None, None, None, None, None),
    ("27", "PICK 2 (Evening)", "2026-10-28", ["7", "0"], WB7, None, None, None, None, None),
    ("32", "PICK 3 (Day)", "2026-10-28", ["3", "3", "1"], WB7, None, None, None, None, None),
    ("28", "PICK 3 (Evening)", "2026-10-28", ["5", "9", "5"], WB7, None, None, None, None, None),
    ("33", "PICK 4 (Day)", "2026-10-28", ["1", "9", "2", "3"], WB7, None, None, None, None, None),
    ("29", "PICK 4 (Evening)", "2026-10-28", ["2", "1", "5", "7"], WB7, None, None, None, None, None),
    ("34", "PICK 5 (Day)", "2026-10-28", ["1", "1", "2", "5", "5"], WB7, None, None, None, None, None),
    ("30", "PICK 5 (Evening)", "2026-10-28", ["1", "2", "0", "6", "2"], WB7, None, None, None, None, None),
    ("7", "Treasure Hunt", "2026-10-28", ["08", "09", "19", "20", "23"], [],
     "2026-10-29", "$52,000", 52000.0, None, None),
    ("8", "Cash 5", "2026-10-28", ["05", "11", "15", "18", "35"], [],
     "2026-10-29", "$1,700,000", 1700000.0, None, None),
    ("11", "Match 6", "2026-10-28", ["01", "03", "19", "22", "39", "45"], [],
     "2026-10-29", "$950,000", 950000.0, None, None),
    ("12", "Powerball", "2026-10-26", ["18", "26", "49", "50", "54"],
     [{"code": "PB", "label": "Powerball", "value": "01"},
      {"code": "PP", "label": "Power Play", "value": "03"}],
     "2026-10-28", "$716,000,000", 716000000.0, "$307,200,000", 307200000.0),
    ("15", "Mega Millions", "2026-10-27", ["14", "19", "21", "45", "67"],
     [{"code": "MB", "label": "Mega Ball", "value": "03"}],
     "2026-10-30", "$463,000,000", 463000000.0, "$194,900,000", 194900000.0),
    ("37", "Millionaire for Life", "2026-10-27", ["13", "21", "22", "23", "33"],
     [{"code": "Mill Ball", "label": "Millionaire Ball", "value": "03"}],
     "2026-10-28", "$1,000,000 A Year For Life", None, None, None),
    ("36", "Powerball Double Play", "2026-10-26", ["05", "11", "14", "24", "59"],
     [{"code": "PB", "label": "Powerball", "value": "08"}],
     "2026-10-28", None, None, "$10,000,000", 10000000.0),
    ("38", "Powerball Xs & Os", "2026-10-25", ["05", "08", "09", "12", "17", "22", "24", "28"], [],
     "2026-11-01", "$2,000,000", 2000000.0, None, None),
]


def _feed(*games: str) -> str:
    return '<?xml version="1.0" encoding="utf-8"?><games>' + "".join(games) + "</games>"


def _game(id="99", title="Test Game", drawdate="10/28/2026", numbers="01 02 03",
          jackpotdate="", jackpot="", cash="") -> str:
    def el(tag, text):
        return f"<{tag}>{text}</{tag}>" if text else f"<{tag} />"
    return (f'<game id="{id}">{el("title", title)}{el("drawdate", drawdate)}'
            f'{el("numbers", numbers)}{el("jackpotdate", jackpotdate)}'
            f'{el("jackpotamount", jackpot)}{el("cashvalue", cash)}</game>')


# --- the real feed --------------------------------------------------------------

def test_every_game_in_the_real_feed_is_read_exactly():
    got = draws.parse_feed(FEED)
    assert len(got) == 16
    for position, (d, want) in enumerate(zip(got, EXPECTED)):
        (gid, title, date, numbers, extras, nxt, jtext, jamount, ctext, camount) = want
        assert (d.game_id, d.title, d.draw_date, d.numbers, d.extras) == (gid, title, date, numbers, extras)
        assert (d.jackpot_date, d.jackpot_text, d.jackpot_amount) == (nxt, jtext, jamount), title
        assert (d.cash_value_text, d.cash_value_amount) == (ctext, camount), title
        assert d.position == position and d.source == "pa_feed"


def test_the_real_feed_reads_with_nothing_to_report():
    reading = draws.read_feed(FEED)
    assert reading.complete and reading.problems == []
    assert reading.order == [g[0] for g in EXPECTED]


def test_raw_keeps_the_game_entry_as_published():
    pb = draws.parse_feed(FEED)[11]
    assert pb.raw.startswith('<game id="12"><title>Powerball</title>')
    assert "<numbers>18 26 49 50 54 PB: 01 PP: 03</numbers>" in pb.raw
    assert pb.raw.endswith("</game>")
    assert "Xs &amp; Os" in draws.parse_feed(FEED)[15].raw      # still valid XML


def test_the_byte_order_mark_is_handled():
    assert FEED.startswith(b"\xef\xbb\xbf")                    # the real file has one
    text = FEED.decode("utf-8")                                # BOM kept as
    assert text.startswith("\ufeff")
    assert [d.game_id for d in draws.parse_feed(text)] == [g[0] for g in EXPECTED]
    assert len(draws.parse_feed(FEED.decode("utf-8-sig"))) == 16    # and without it


def test_empty_elements_mean_none():
    d = draws.parse_feed(_feed(_game()))[0]
    assert (d.jackpot_date, d.jackpot_text, d.jackpot_amount) == (None, None, None)
    assert (d.cash_value_text, d.cash_value_amount) == (None, None)
    # whitespace only is empty too
    d = draws.parse_feed(_feed(_game(jackpot="   ")))[0]
    assert d.jackpot_text is None


def test_leading_zeros_are_kept():
    d = draws.parse_feed(_feed(_game(numbers="01 02 09 PB: 04")))[0]
    assert d.numbers == ["01", "02", "09"] and d.extra("PB") == "04"


# --- extras ---------------------------------------------------------------------

def test_known_extra_labels_get_readable_names():
    d = draws.parse_feed(_feed(_game(numbers="1 2 3 WB: 4 PB: 05 PP: 2 MB: 06 Mill Ball: 07")))[0]
    assert [(e["code"], e["label"], e["value"]) for e in d.extras] == [
        ("WB", "Wild Ball", "4"), ("PB", "Powerball", "05"), ("PP", "Power Play", "2"),
        ("MB", "Mega Ball", "06"), ("Mill Ball", "Millionaire Ball", "07")]


def test_an_unknown_extra_label_is_kept_and_reported():
    reading = draws.read_feed(_feed(_game(title="Cash Pop", numbers="05 11 Bonus Ball: 04")))
    d = reading.draws[0]
    assert d.numbers == ["05", "11"]
    assert d.extras == [{"code": "Bonus Ball", "label": "Bonus Ball", "value": "04"}]
    assert reading.complete                        # nothing was left out
    assert len(reading.problems) == 1
    assert "Cash Pop" in reading.problems[0] and "Bonus Ball" in reading.problems[0]
    assert draws.parse_feed(_feed(_game(numbers="05 11 Bonus Ball: 04")))   # not an error


# --- anything that can't be read exactly is left out ------------------------------

@pytest.mark.parametrize("numbers", [
    "18 26 49 50 54 PB 01",          # label without a colon
    "18 26 4x 50 54",                # not a number
    "18, 26, 49, 50, 54",            # commas
    "18 26 49 50 154",               # three digits
    "PB: 01",                        # no main numbers
    "",                              # nothing at all
    "18 26 49 50 54 PB: 01 PB: 02",  # the same ball twice
    "18 26 49 50 54 PB: 012",        # bonus too long
    "18 26 49 50 54 PB: ",           # bonus missing
    "18 26 49 50 54 PB: 01 junk",    # trailing text
    "18 26 49 50 54 2X: 01",         # label that isn't a word
])
def test_a_malformed_numbers_string_is_rejected(numbers):
    feed = _feed(_game(id="12", title="Powerball", numbers=numbers), _game(id="8", title="Cash 5"))
    reading = draws.read_feed(feed)
    assert [d.game_id for d in reading.draws] == ["8"]           # the rest still read
    assert not reading.complete
    assert any("Powerball" in p and "left out" in p for p in reading.problems)
    assert reading.order == ["12", "8"]                          # its place is kept
    with pytest.raises(FeedError):
        draws.parse_feed(feed)


@pytest.mark.parametrize("drawdate", ["13/45/2026", "2026-10-28", "10/28/26", "02/30/2026", ""])
def test_a_bad_draw_date_is_rejected(drawdate):
    reading = draws.read_feed(_feed(_game(drawdate=drawdate)))
    assert reading.draws == [] and not reading.complete
    with pytest.raises(FeedError):
        draws.parse_feed(_feed(_game(drawdate=drawdate)))


def test_a_bad_next_draw_date_is_left_out_not_guessed():
    reading = draws.read_feed(_feed(_game(jackpotdate="Thursday", jackpot="$1,000")))
    d = reading.draws[0]
    assert d.jackpot_date is None and d.jackpot_amount == 1000.0
    assert not reading.complete and "next draw" in reading.problems[0]


def test_a_game_without_an_id_or_name_is_left_out():
    reading = draws.read_feed(_feed(_game(id=""), _game(id="5", title="")))
    assert reading.draws == [] and len(reading.problems) == 2


def test_a_game_listed_twice_is_left_out():
    reading = draws.read_feed(_feed(_game(id="8", numbers="1 2"), _game(id="8", numbers="3 4"),
                                    _game(id="9")))
    assert [d.game_id for d in reading.draws] == ["9"]
    assert "twice" in reading.problems[0]


@pytest.mark.parametrize("text", [
    "<html><body>Service unavailable</body></html>",   # an error page
    "not xml at all",
    '<?xml version="1.0"?><games></games>',             # no games
    '<?xml version="1.0"?><games><game id="1">',        # cut off
    b'<?xml version="1.0" encoding="bogus"?><games><game id="1" /></games>',   # unknown encoding
])
def test_a_document_that_isnt_the_feed_fails_loudly(text):
    with pytest.raises(FeedError):
        draws.read_feed(text)


@pytest.mark.parametrize("doctype", [
    # Entities that expand into each other (a "billion laughs", kept small here)
    '<!DOCTYPE games [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>',
    '<!DOCTYPE games [<!ENTITY x SYSTEM "file:///etc/passwd">]>',   # a file from the machine
    '<!doctype games SYSTEM "http://example.com/games.dtd">',
])
def test_a_feed_that_declares_entities_is_refused_not_expanded(doctype):
    # PA's feed has no DOCTYPE. One showing up is refused before any entity
    # in it is expanded, whatever XML library is installed.
    text = ('<?xml version="1.0" encoding="utf-8"?>' + doctype
            + "<games>" + _game(title="&b;" if "ENTITY a" in doctype else "Test") + "</games>")
    with pytest.raises(FeedError, match="document type"):
        draws.read_feed(text)
    with pytest.raises(FeedError, match="document type"):
        draws.read_feed("﻿" + text)
    with pytest.raises(FeedError, match="document type"):
        draws.read_feed(text.encode("utf-8"))


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_a_document_type_is_refused_in_any_encoding(encoding):
    # In UTF-16 "<!DOCTYPE" isn't those bytes, so looking for the text wasn't
    # enough: the XML reader decodes it and would have expanded the entities.
    text = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE games [<!ENTITY a "aaaaaaaaaa">'
            '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><games>' + _game(title="&b;")
            + "</games>")
    with pytest.raises(FeedError, match="document type"):
        draws.read_feed(text.encode(encoding))
    # the same feed without the document type still reads, in that encoding
    plain = text.replace(text[text.index("<!DOCTYPE"):text.index("<games>")], "")
    assert draws.read_feed(plain.replace("&b;", "Test").encode(encoding)).draws[0].title == "Test"


# --- strict money ----------------------------------------------------------------

@pytest.mark.parametrize("text,amount", [
    ("$716,000,000", 716000000.0),
    ("$52,000", 52000.0),
    ("$5", 5.0),
    ("$1000", 1000.0),
    ("$1,234.50", 1234.5),
    (" $950,000 ", 950000.0),
    ("$1,000,000 A Year For Life", None),
    ("$1,00,000", None),
    ("$12,34", None),
    ("716000000", None),
    ("$ 5", None),
    ("$1,000.5", None),
    ("$716 Million", None),
    ("", None),
    (None, None),
])
def test_money_is_read_strictly(text, amount):
    assert draws.money_amount(text) == amount


@pytest.mark.parametrize("text,amount", [
    ("$463 Million", 463000000.0),
    ("$716 Million", 716000000.0),
    ("$1.5 Billion", 1500000000.0),
    ("$1.23 billion", 1230000000.0),
    ("$463M", None),
    ("$463,000,000", None),
    ("About $463 Million", None),
    ("$463 Million (est.)", None),
    ("", None),
])
def test_the_old_scrapers_jackpots_are_read_strictly(text, amount):
    assert draws.millions_amount(text) == amount


# --- the old scraper's lottery.json ------------------------------------------------

def test_the_old_scrapers_real_file_is_read_exactly():
    got = {d.game_id: d for d in draws.parse_old_scraper(OLD)}
    assert set(got) == {"12", "15"}
    pb, mm = got["12"], got["15"]
    assert pb == Draw(game_id="12", title="Powerball", draw_date="2026-10-26",
                      numbers=["18", "26", "49", "50", "54"],
                      extras=[{"code": "PB", "label": "Powerball", "value": "01"}],
                      jackpot_date="2026-10-28", jackpot_text="$716 Million",
                      jackpot_amount=716000000.0, raw=pb.raw, position=None, source="old_scraper")
    assert mm == Draw(game_id="15", title="Mega Millions", draw_date="2026-10-27",
                      numbers=["14", "19", "21", "45", "67"],
                      extras=[{"code": "MB", "label": "Mega Ball", "value": "03"}],
                      jackpot_date="2026-10-30", jackpot_text="$463 Million",
                      jackpot_amount=463000000.0, raw=mm.raw, position=None, source="old_scraper")
    assert '"nextJackpot": "$716 Million"' in pb.raw


def test_the_old_scraper_agrees_with_pa_on_the_same_draws():
    pa = {d.game_id: d for d in draws.parse_feed(FEED)}
    for old in draws.parse_old_scraper(OLD):
        same = pa[old.game_id]
        assert old.draw_date == same.draw_date and old.numbers == same.numbers
        assert old.extras[0] == same.extras[0]
        assert old.jackpot_amount == same.jackpot_amount and old.jackpot_date == same.jackpot_date


def test_the_old_scrapers_file_with_a_bom():
    assert len(draws.parse_old_scraper("\ufeff" + OLD)) == 2
    assert len(draws.parse_old_scraper(b"\xef\xbb\xbf" + OLD.encode())) == 2


def _old(**pb):
    import json
    base = {"numbers": [18, 26, 49, 50, 54, 1], "date": "October 26, 2026 Drawing",
            "nextDrawing": "Wednesday, October 28, 2026", "nextJackpot": "$716 Million"}
    base.update(pb)
    mm = {"numbers": [14, 19, 21, 45, 67, 3], "date": "October 27, 2026 Drawing",
          "nextDrawing": "Friday, October 30, 2026", "nextJackpot": "$463 Million"}
    return json.dumps({"powerball": base, "megaMillions": mm})


@pytest.mark.parametrize("change", [
    {"numbers": [18, 26, 49, 50, 54]},                # no bonus ball
    {"numbers": [18, 26, 49, 50, 54, 1, 2]},          # one too many
    {"numbers": ["18", "26", "49", "50", "54", "1"]},  # text, not numbers
    {"numbers": [18, 26, 49, 50, 54, True]},
    {"numbers": [18, 26, 49, 50, 0, 1]},
    {"date": "10/26/2026"},
    {"date": "October 26, 2026"},                     # no "Drawing"
    {"date": None},
])
def test_an_old_scraper_entry_that_cant_be_read_is_left_out(change):
    reading = draws.read_old_scraper(_old(**change))
    assert [d.game_id for d in reading.draws] == ["15"]
    assert not reading.complete and "Powerball" in reading.problems[0]
    with pytest.raises(FeedError):
        draws.parse_old_scraper(_old(**change))


def test_an_old_scraper_next_draw_with_the_wrong_weekday_is_left_out():
    reading = draws.read_old_scraper(_old(nextDrawing="Thursday, October 28, 2026"))
    pb = [d for d in reading.draws if d.game_id == "12"][0]
    assert pb.jackpot_date is None and not reading.complete


def test_an_old_scraper_jackpot_that_isnt_millions_keeps_its_text():
    pb = [d for d in draws.read_old_scraper(_old(nextJackpot="$20 Million Annuity")).draws
          if d.game_id == "12"][0]
    assert pb.jackpot_text == "$20 Million Annuity" and pb.jackpot_amount is None


def test_an_old_scraper_file_that_isnt_json_fails_loudly():
    with pytest.raises(FeedError):
        draws.read_old_scraper("<html>404</html>")
    with pytest.raises(FeedError):
        draws.read_old_scraper("[1, 2]")
    reading = draws.read_old_scraper("{}")
    assert reading.draws == [] and len(reading.problems) == 2
