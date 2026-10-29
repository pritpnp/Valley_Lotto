"""PA's weekly "Scratch-Offs Available for Ordering" list: found, read whole or
not at all, saved once per "As of" date, and announced when it changes.

Uses the real retailer page and PDF fetched on 2026-10-28 (tests/fixtures/pa/).
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from lottery_tracker import telegram
from lottery_tracker.model import Game
from lottery_tracker.pa_feeds import __main__ as job
from lottery_tracker.pa_feeds import orders, store
from lottery_tracker.pa_feeds.orders import OrderListError
from lottery_tracker.web.models import OrderGameRow, OrderListRow

PA = Path(__file__).parent / "fixtures" / "pa"
PAGE = (PA / "Current-Retailers_2026-10-28.html").read_bytes()
PDF = (PA / "Scratch-Offs_Order_10-26-26.pdf").read_bytes()
PDF_URL = ("https://www.palottery.pa.gov/PaLotteryWebSite/media/Retailer-Documents/"
           "Retailer-Tools/Scratch-Offs_Order/Scratch-Offs_Order_10-26-26.pdf")

# The real PDF, row by row: game number, price, tickets per pack, name.
ROWS = """\
1806|1|100|CASH GRAB
1801|1|100|THE WIZARD OF OZ GLINDA THE GOOD WITCH
1795|1|100|BRIGHT BUCK$
1783|1|100|GOLDFISH
1779|1|100|A LATTE MONEY
1773|1|100|5 STAR WINS
1767|1|100|$500 a WEEK FOR LIFE
1805|2|100|GHOSTBUSTERS
1800|2|100|WINNER WINNER CHICKEN DINNER
1794|2|100|$20 FRENZY
1789|2|100|SUPER 7S
1778|2|100|MONEY RUSH
1788|3|100|LIGHTS, CAMERA, CROSSWORD
1753|3|100|CROSSWORD EXTRA
1740|3|100|BONUS BALL BINGO
1804|5|60|FAT STACKS
1799|5|60|THE WIZARD OF OZ YELLOW BRICK ROAD
1793|5|60|CROSSWORD DELUXE
1792|5|60|WILD SIDE
1787|5|60|FEVER DOUBLER
1782|5|60|GUS
1777|5|60|MONEY BOX BINGO
1770|5|60|PHILADELPHIA 76ERS
1765|5|60|$10,000 a MONTH FOR LIFE
1739|5|60|MONOPOLY SECRET VAULT
1803|10|60|CANDY-COATED CA$H
1798|10|60|THE WIZARD OF OZ THE GREAT AND POWERFUL OZ
1791|10|60|VIP BONUS CASH
1786|10|60|RUBY RICH3S
1781|10|60|WIN WIN WIN
1775|10|60|$250 BLOWOUT
1769|10|60|CODE WORD CROSSWORD
1764|10|60|$20,000 a MONTH FOR LIFE
1751|10|60|EXTREME GREEN
1747|10|60|JURASSIC PARK
1735|10|60|SIX FIGURES
1724|10|60|LADY LUCK
1719|10|60|FAST $1,000
1797|20|30|SNAKE, RATTLE AND ROLL
1785|20|30|THE GAME OF LIFE
1774|20|30|$500 BLOWOUT
1763|20|30|$500,000 a YEAR FOR LIFE
1746|20|30|MILLIONAIRE LOADING
1802|30|30|GAME SHOW EXPERIENCE
1790|30|30|$1,500,000 SUPER STAR
1780|30|30|TRIPLE RED 777
1768|30|30|$120,000,000 PAYOUT
1750|30|30|CASH SPECTACULAR
1742|30|30|$3 MILLION MEGA MOOLAH
1734|30|30|3s A CHARM
1723|30|30|THE HUNT FOR $3,000,000
1796|50|50|CA$H MONEY
1784|50|50|MILLION DOLLAR WIN IT ALL
1762|50|50|$1,000,000 a YEAR FOR LIFE
1717|50|50|$5,000,000 LION'S SHARE"""
EXPECTED = [orders.OrderGame(n, float(p), int(t), name)
            for n, p, t, name in (line.split("|") for line in ROWS.splitlines())]

HEADING = "Price # Tix/Pack GAMES AVAILABLE TO ORDER"


def make_pdf(*pages: list[str]) -> bytes:
    """A small, real PDF: each page is lines of text, top to bottom."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    n_pages = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n_pages))
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>"]
    for i, lines in enumerate(pages):
        ops = ["BT", "/F1 9 Tf", "36 760 Td"]
        for line in lines:
            ops += [f"({esc(line)}) Tj", "0 -12 Td"]
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>")
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objs, 1):
        offsets.append(len(out))
        body = body.encode("latin-1") if isinstance(body, str) else body
        out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def order_pdf(as_of="10/26/26", rows=("1806 $1 100 CASH GRAB", "1717 $50 50 $5,000,000 LION'S SHARE"),
              heading=HEADING) -> bytes:
    return make_pdf(["As of", f"{as_of} {heading}", *rows])


# --- the link -----------------------------------------------------------------------

def test_the_order_link_is_found_in_the_real_retailer_page():
    assert orders.find_order_link(PAGE, job.RETAILERS_URL) == PDF_URL
    assert orders.find_order_link(PAGE.decode("utf-8"), job.RETAILERS_URL) == PDF_URL


@pytest.mark.parametrize("anchor", [
    '<a href="/x/List_11-02-26.pdf">Scratch-Offs Available for Ordering</a>',
    '<a href="/x/List_11-02-26.pdf">Scratch-Offs&nbsp;Available  for\n Ordering&nbsp;<i>&nbsp;</i></a>',
    '<a href="/x/List_11-02-26.pdf">SCRATCH-OFFS AVAILABLE FOR ORDERING</a>',
    '<a href="/x/List_11-02-26.pdf"><span>Scratch-Offs</span> Available for <b>Ordering</b></a>',
])
def test_the_link_is_matched_on_its_words(anchor):
    page = f"<html><body><a href='/other.pdf'>Order forms</a>{anchor}</body></html>"
    assert orders.find_order_link(page, "https://www.palottery.pa.gov/a/b/Page.aspx") == \
        "https://www.palottery.pa.gov/x/List_11-02-26.pdf"


def test_relative_and_absolute_links_resolve():
    rel = '<a href="files/L.pdf">Scratch-Offs Available for Ordering</a>'
    assert orders.find_order_link(rel, "https://pa.gov/dir/Page.aspx") == "https://pa.gov/dir/files/L.pdf"
    absolute = '<a href="https://cdn.example/L.pdf">Scratch-Offs Available for Ordering</a>'
    assert orders.find_order_link(absolute, "https://pa.gov/dir/") == "https://cdn.example/L.pdf"


def test_no_link_is_an_error():
    with pytest.raises(OrderListError):
        orders.find_order_link("<a href='/x.pdf'>Scratch-Offs Available</a>", "https://pa.gov/")
    with pytest.raises(OrderListError):
        orders.find_order_link("<html></html>", "https://pa.gov/")


def test_two_different_links_are_an_error_not_a_guess():
    page = ('<a href="/a.pdf">Scratch-Offs Available for Ordering</a>'
            '<a href="/b.pdf">Scratch-Offs Available for Ordering</a>')
    with pytest.raises(OrderListError):
        orders.find_order_link(page, "https://pa.gov/")
    same = '<a href="/a.pdf">Scratch-Offs Available for Ordering</a>' * 2
    assert orders.find_order_link(same, "https://pa.gov/") == "https://pa.gov/a.pdf"


@pytest.mark.parametrize("url,iso", [
    (PDF_URL, "2026-10-26"),
    ("https://x/Scratch-Offs_Order_1-5-27.pdf", "2027-01-05"),
    ("https://x/Scratch-Offs_Order_11-02-2026.PDF", "2026-11-02"),
    ("https://x/Scratch-Offs_Order_10-26-26.pdf?v=2", "2026-10-26"),
    ("https://x/Scratch-Offs_Order.pdf", None),
    ("https://x/Scratch-Offs_Order_13-40-26.pdf", None),
])
def test_the_date_in_the_link(url, iso):
    assert orders.url_date(url) == iso


# --- the real PDF ----------------------------------------------------------------------

def test_the_real_pdf_is_read_exactly():
    got = orders.parse_order_pdf(PDF)
    assert got.as_of == "2026-10-26"
    assert len(got.games) == 55
    assert got.games == EXPECTED


def test_the_real_pdf_agrees_with_the_tracker():
    """Every game on it is one the tracker knows, at the same price."""
    from lottery_tracker.state import load_state
    known = load_state(Path(__file__).resolve().parents[1] / "data" / "state.json")
    assert orders.cross_check(orders.parse_order_pdf(PDF), PDF_URL, known) == []


# --- a PDF that has changed is rejected whole -------------------------------------------

def test_a_hand_made_pdf_reads_like_the_real_one():
    got = orders.parse_order_pdf(order_pdf())
    assert got.as_of == "2026-10-26"
    assert got.games == [orders.OrderGame("1806", 1.0, 100, "CASH GRAB"),
                         orders.OrderGame("1717", 50.0, 50, "$5,000,000 LION'S SHARE")]


@pytest.mark.parametrize("heading", [
    "Price # Tickets GAMES AVAILABLE TO ORDER",          # Tix/Pack renamed
    "Cost # Tix/Pack GAMES AVAILABLE TO ORDER",          # Price renamed
    "Price # Tix/Pack GAMES",                            # title cut
    "# Tix/Pack Price GAMES AVAILABLE TO ORDER",         # columns swapped
    "GAMES AVAILABLE TO ORDER Price # Tix/Pack",
    "Price # Tix/Pack Top Prize GAMES AVAILABLE TO ORDER",   # a column added
    "Price # Tix/Pack Launch GAMES AVAILABLE TO ORDER",
    "Game Price # Tix/Pack GAMES AVAILABLE TO ORDER",
    "Price # Tix/Pack GAMES AVAILABLE TO ORDER Notes",
])
def test_a_pdf_with_a_changed_header_is_rejected(heading):
    # The rows read the same either way ("1806 $1 100 $5,000 CASH GRAB" would
    # pass as a game called "$5,000 CASH GRAB"), so the heading is what tells.
    with pytest.raises(OrderListError, match="heading"):
        orders.parse_order_pdf(order_pdf(heading=heading, rows=("1806 $1 100 $5,000 CASH GRAB",)))


def test_the_heading_may_carry_its_as_of_words_on_the_same_line():
    got = orders.parse_order_pdf(make_pdf([f"As of 10/26/26 {HEADING}", "1806 $1 100 CASH GRAB"]))
    assert (got.as_of, [g.name for g in got.games]) == ("2026-10-26", ["CASH GRAB"])


@pytest.mark.parametrize("row", [
    "1806 100 $1 CASH GRAB",              # columns moved
    "1806 $1 CASH GRAB",                  # tickets per pack missing
    "1806 $1.5 100 CASH GRAB",            # price not exact
    "1806 $1 0 CASH GRAB",                # no tickets in a pack
    "18O6 $1 100 CASH GRAB",              # letter O, not zero
    "1806 $1 100",                        # no name
    "Page 1 of 2",                        # anything else
    "18061806180618061 $1 100 CASH GRAB",     # a game number longer than the database holds
    "1806 $1 100 " + "CASH GRAB " * 26,       # a name longer than the database holds
])
def test_one_bad_row_rejects_the_whole_list(row):
    with pytest.raises(OrderListError):
        orders.parse_order_pdf(order_pdf(rows=("1801 $1 100 GLINDA", row, "1717 $50 50 LION")))


def test_the_longest_row_allowed_is_what_the_database_holds():
    cols = OrderGameRow.__table__.c
    assert (orders.LONGEST_NUMBER, orders.LONGEST_NAME) == (cols.game_number.type.length,
                                                            cols.name.type.length)


def test_zero_rows_is_rejected():
    with pytest.raises(OrderListError, match="no games"):
        orders.parse_order_pdf(order_pdf(rows=()))


@pytest.mark.parametrize("pages", [
    (["Scratch-Offs", HEADING, "1806 $1 100 CASH GRAB"],),                       # no As of
    (["As of", f"10/32/26 {HEADING}", "1806 $1 100 CASH GRAB"],),                # bad date
    (["As of", f"10/26/26 {HEADING}", "1806 $1 100 A"], ["As of", f"11/02/26 {HEADING}", "1717 $50 50 B"]),
])
def test_the_as_of_date_must_be_there_and_be_one(pages):
    with pytest.raises(OrderListError, match="As of"):
        orders.parse_order_pdf(make_pdf(*pages))


def test_a_game_listed_twice_is_rejected():
    with pytest.raises(OrderListError, match="more than once"):
        orders.parse_order_pdf(order_pdf(rows=("1806 $1 100 A", "1806 $1 100 A")))


def test_games_above_the_heading_are_rejected():
    with pytest.raises(OrderListError):
        orders.parse_order_pdf(make_pdf(["1806 $1 100 CASH GRAB", "As of", f"10/26/26 {HEADING}"]))


def test_a_title_above_the_heading_and_a_heading_on_every_page_are_fine():
    got = orders.parse_order_pdf(make_pdf(
        ["PENNSYLVANIA LOTTERY", "As of", f"10/26/26 {HEADING}", "1806 $1 100 CASH GRAB"],
        ["As of", f"10/26/26 {HEADING}", "1805 $2 100 GHOSTBUSTERS"]))
    assert [g.game_number for g in got.games] == ["1806", "1805"]


def test_a_game_with_a_heading_word_in_its_name_is_still_a_game():
    got = orders.parse_order_pdf(order_pdf(rows=("1810 $5 60 THE PRICE IS RIGHT",
                                                 "1811 $10 60 GAMES AVAILABLE TO ORDER")))
    assert [g.name for g in got.games] == ["THE PRICE IS RIGHT", "GAMES AVAILABLE TO ORDER"]


def test_a_four_digit_year_reads_too():
    assert orders.parse_order_pdf(order_pdf(as_of="11/02/2026")).as_of == "2026-11-02"


def test_something_that_isnt_a_pdf_is_rejected():
    with pytest.raises(OrderListError):
        orders.parse_order_pdf(b"<html>Not found</html>")


# --- cross-checks: worth a look, but the list is still used ---------------------------------

def test_cross_check_notes():
    lst = orders.OrderList("2026-10-26", [orders.OrderGame("1806", 1.0, 100, "CASH GRAB"),
                                          orders.OrderGame("1717", 50.0, 50, "LION'S SHARE"),
                                          orders.OrderGame("1900", 5.0, 60, "BRAND NEW")])
    known = {"1806": Game(game_number="1806", name="Cash Grab", price=2.0),
             "1717": Game(game_number="1717", name="Lion", price=50.0)}
    notes = orders.cross_check(lst, "https://x/Scratch-Offs_Order_10-19-26.pdf", known)
    assert len(notes) == 3
    assert "2026-10-19" in notes[0] and "2026-10-26" in notes[0]
    assert "#1806" in notes[1] and "$1" in notes[1] and "$2" in notes[1]
    assert "#1900 BRAND NEW ($5)" in notes[2]
    # no tracker data: only the link is checked
    assert orders.cross_check(lst, PDF_URL, {}) == []


# --- the orders job, start to finish ---------------------------------------------------------

T0 = datetime(2026, 10, 28, 12, 7, tzinfo=timezone.utc)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'feeds.db'}")
    store.create_tables(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture()
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(telegram, "send", lambda text, formatted=False: out.append(text) or True)
    return out


@pytest.fixture()
def no_tracker(monkeypatch):
    """Made-up games aren't in data/state.json; leave that cross-check out."""
    monkeypatch.setattr(job, "_known_games", lambda: {})


class Web:
    """Stands in for the internet: url -> bytes, or an exception to raise."""

    def __init__(self, monkeypatch, pages):
        self.pages, self.asked = dict(pages), []
        monkeypatch.setattr(job.requests, "get", self.get)
        monkeypatch.setattr(job, "RETRY_WAIT", 0)

    def get(self, url, headers=None, timeout=None):
        self.asked.append(url)
        assert "Mozilla" in headers["User-Agent"] and timeout
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return Resp(page)


class Resp:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        pass


def retailer_page(href):
    return (f'<html><a href="{href}">Scratch-Offs Available for Ordering&nbsp;'
            f'<i>&nbsp;</i></a></html>').encode()


def test_the_first_list_is_saved_and_announced_with_its_count(db, sent, monkeypatch):
    web = Web(monkeypatch, {job.RETAILERS_URL: PAGE, PDF_URL: PDF})
    job.orders(db, T0)
    row = db.get(OrderListRow, "2026-10-26")
    assert row.url == PDF_URL and row.games == 55 and row.pdf == PDF
    assert row.pdf_sha256 == store.pdf_hash(PDF)
    saved = db.scalars(select(OrderGameRow).filter_by(as_of="2026-10-26")).all()
    assert len(saved) == 55
    assert {(g.game_number, g.price, g.tickets_per_pack, g.name) for g in saved} == \
        {(g.game_number, g.price, g.tickets_per_pack, g.name) for g in EXPECTED}
    assert len(sent) == 1 and "55 games" in sent[0] and "Oct 26, 2026" in sent[0]
    assert web.asked == [job.RETAILERS_URL, PDF_URL]


def test_the_same_link_is_not_downloaded_again(db, sent, monkeypatch):
    web = Web(monkeypatch, {job.RETAILERS_URL: PAGE, PDF_URL: PDF})
    job.orders(db, T0)
    job.orders(db, T0 + timedelta(hours=1))
    assert web.asked == [job.RETAILERS_URL, PDF_URL, job.RETAILERS_URL]
    assert len(sent) == 1
    assert db.scalar(select(func.count()).select_from(OrderListRow)) == 1


def test_a_new_week_announces_games_added_and_dropped(db, sent, monkeypatch, no_tracker):
    week1 = "https://pa.gov/Scratch-Offs_Order_10-26-26.pdf"
    week2 = "https://pa.gov/Scratch-Offs_Order_11-02-26.pdf"
    Web(monkeypatch, {job.RETAILERS_URL: retailer_page(week1),
                      week1: order_pdf(rows=("1806 $1 100 CASH GRAB", "1783 $1 100 GOLDFISH",
                                             "1717 $50 50 LION'S SHARE")),
                      week2: order_pdf(as_of="11/02/26", rows=(
                          "1806 $1 100 CASH GRAB", "1717 $50 50 LION'S SHARE",
                          "1810 $5 60 PRICE & <PRIZE>"))})
    job.orders(db, T0)
    assert len(sent) == 1 and "3 games" in sent[0]

    Web(monkeypatch, {job.RETAILERS_URL: retailer_page(week2),
                      week2: order_pdf(as_of="11/02/26", rows=(
                          "1806 $1 100 CASH GRAB", "1717 $50 50 LION'S SHARE",
                          "1810 $5 60 PRICE & <PRIZE>"))})
    job.orders(db, T0 + timedelta(days=7))
    assert len(sent) == 2
    msg = sent[1]
    assert "Added (1)" in msg and "<b>PRICE &amp; &lt;PRIZE&gt; #1810</b>" in msg and "<u>$5</u>" in msg
    assert "no longer ships these (1)" in msg and "GOLDFISH #1783" in msg and "<u>$1</u>" in msg
    assert "CASH GRAB" not in msg
    assert db.scalar(select(func.count()).select_from(OrderListRow)) == 2


def test_a_new_week_with_the_same_games_says_nothing(db, sent, monkeypatch, no_tracker):
    week1 = "https://pa.gov/Scratch-Offs_Order_10-26-26.pdf"
    week2 = "https://pa.gov/Scratch-Offs_Order_11-02-26.pdf"
    Web(monkeypatch, {job.RETAILERS_URL: retailer_page(week1), week1: order_pdf()})
    job.orders(db, T0)
    Web(monkeypatch, {job.RETAILERS_URL: retailer_page(week2), week2: order_pdf(as_of="11/02/26")})
    job.orders(db, T0 + timedelta(days=7))
    assert len(sent) == 1
    assert db.get(OrderListRow, "2026-11-02") is not None


def test_the_same_as_of_with_a_different_pdf_saves_nothing_and_is_reported_once(db, sent, monkeypatch, no_tracker):
    Web(monkeypatch, {job.RETAILERS_URL: PAGE, PDF_URL: PDF})
    job.orders(db, T0)
    assert len(sent) == 1

    other = "https://pa.gov/Scratch-Offs_Order_10-26-26-revised.pdf"
    changed = order_pdf(rows=("1806 $1 100 CASH GRAB",))          # also "As of 10/26/26"
    web = Web(monkeypatch, {job.RETAILERS_URL: retailer_page(other), other: changed})
    job.orders(db, T0 + timedelta(hours=1))
    job.orders(db, T0 + timedelta(hours=2))
    assert len(sent) == 2                                           # once, not twice
    assert web.asked.count(other) == 1                              # and read once
    assert "different order list" in sent[1] and "Oct 26, 2026" in sent[1]
    row = db.get(OrderListRow, "2026-10-26")
    assert row.pdf == PDF and row.games == 55                       # the first one is kept
    assert db.scalar(select(func.count()).select_from(OrderGameRow)) == 55


def test_a_link_without_a_date_is_read_once(db, sent, monkeypatch, no_tracker):
    url = "https://pa.gov/Scratch-Offs_Order.pdf"
    web = Web(monkeypatch, {job.RETAILERS_URL: retailer_page(url), url: order_pdf()})
    for hours in range(3):
        job.orders(db, T0 + timedelta(hours=hours))
    assert web.asked.count(url) == 1 and len(sent) == 1


def test_a_changed_pdf_is_reported_once_and_fixed_when_it_reads_again(db, sent, monkeypatch, no_tracker):
    url = "https://pa.gov/Scratch-Offs_Order_10-26-26.pdf"
    bad = order_pdf(heading="Price # Tickets GAMES AVAILABLE TO ORDER")
    web = Web(monkeypatch, {job.RETAILERS_URL: retailer_page(url), url: bad})
    job.orders(db, T0)
    job.orders(db, T0 + timedelta(hours=1))                      # tried again, not re-sent
    assert len(sent) == 1 and "wasn't used" in sent[0] and "heading" in sent[0]
    assert web.asked.count(url) == 2
    assert db.scalar(select(func.count()).select_from(OrderListRow)) == 0

    web.pages[url] = order_pdf()
    job.orders(db, T0 + timedelta(hours=2))
    assert "reads normally again" in sent[1] and "2 games" in sent[2]


def test_a_missing_link_is_reported_once_after_an_hour(db, sent, monkeypatch):
    # A page without the link may be a busy page or a robot check rather than a
    # redesign, so it's treated like a page that didn't answer.
    web = Web(monkeypatch, {job.RETAILERS_URL: b"<html>redesigned</html>"})
    job.orders(db, T0)
    job.orders(db, T0 + timedelta(minutes=59))
    assert sent == []
    job.orders(db, T0 + timedelta(minutes=61))
    job.orders(db, T0 + timedelta(minutes=121))
    assert len(sent) == 1 and "Can't read PA's retailer page" in sent[0]
    assert "no longer has a “Scratch-Offs Available for Ordering” link" in sent[0]
    assert web.asked == [job.RETAILERS_URL] * 4
    web.pages = {job.RETAILERS_URL: PAGE, PDF_URL: PDF}
    job.orders(db, T0 + timedelta(minutes=181))
    assert sent[1] == store.FIXED["orders_page_fetch"] and "55 games" in sent[2]


def test_a_retailer_page_that_comes_and_goes_is_not_a_message_every_hour(db, sent, monkeypatch,
                                                                         no_tracker):
    web = Web(monkeypatch, {PDF_URL: PDF})
    for hours in range(12):
        web.pages[job.RETAILERS_URL] = PAGE if hours % 2 else b"<html>Checking your browser</html>"
        job.orders(db, T0 + timedelta(hours=hours))
    assert len(sent) == 1 and "55 games" in sent[0]             # just the list itself


def test_a_pdf_link_that_answers_with_a_web_page_is_like_no_answer(db, sent, monkeypatch, no_tracker):
    web = Web(monkeypatch, {job.RETAILERS_URL: PAGE, PDF_URL: b"<html>Checking your browser</html>"})
    job.orders(db, T0)
    job.orders(db, T0 + timedelta(minutes=59))
    assert sent == []
    web.pages[PDF_URL] = PDF                                     # next try: the real PDF
    job.orders(db, T0 + timedelta(minutes=119))
    assert len(sent) == 1 and "55 games" in sent[0]
    web.pages[PDF_URL] = b"<html>Checking your browser</html>"   # never asked again
    job.orders(db, T0 + timedelta(minutes=179))
    assert web.asked.count(PDF_URL) == 3


def test_the_retailer_page_being_down_is_only_mentioned_after_an_hour(db, sent, monkeypatch):
    import requests
    web = Web(monkeypatch, {job.RETAILERS_URL: requests.ConnectionError("down")})
    job.orders(db, T0)
    job.orders(db, T0 + timedelta(minutes=59))
    assert sent == []
    job.orders(db, T0 + timedelta(minutes=61))
    job.orders(db, T0 + timedelta(minutes=121))
    assert len(sent) == 1 and "retailer page" in sent[0] and "ConnectionError" in sent[0]
    web.pages = {job.RETAILERS_URL: PAGE, PDF_URL: PDF}
    job.orders(db, T0 + timedelta(minutes=181))
    assert sent[1] == store.FIXED["orders_page_fetch"] and "55 games" in sent[2]


def test_cross_check_notes_are_sent_with_the_new_list(db, sent, monkeypatch, no_tracker):
    url = "https://pa.gov/Scratch-Offs_Order_10-19-26.pdf"            # link says 10/19
    Web(monkeypatch, {job.RETAILERS_URL: retailer_page(url), url: order_pdf()})
    job.orders(db, T0)
    assert len(sent) == 2 and "worth a look" in sent[1] and "2026-10-19" in sent[1]
