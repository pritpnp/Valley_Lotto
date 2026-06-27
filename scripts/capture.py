"""Investigate whether PA exposes per-prize odds / full prize structure (all tiers'
original counts), which is what's needed to compute Jackpot Density / weighted
low-prize health. Dumps detail-page anchors + odds patterns to debug/."""
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bs4 import BeautifulSoup
from lottery_tracker import fetch

DEBUG = Path("debug"); DEBUG.mkdir(exist_ok=True)
# A few games incl. High 5 (1736) which Win PA Lottery rates high jackpot-density.
IDS = ["3363", "3340"]
GUESS_URLS = [
    "https://www.palottery.pa.gov/Scratch-Offs/Prizes-Chances.aspx?id={id}",
    "https://www.palottery.pa.gov/Scratch-Offs/Game-Rules.aspx?id={id}",
]

def dump(name, html):
    (DEBUG / f"{name}.html").write_text(html)
    soup = BeautifulSoup(html, "html.parser")
    text = " ".join(soup.get_text(" ").split())
    odds = re.findall(r"1 in [0-9.,]+|1:[0-9.]+", text)
    print(f"\n=== {name} ({len(html)}b) ===")
    print(" odds tokens:", odds[:20])
    # links that look like prize/odds/rules/pdf
    hrefs = sorted({a.get("href","") for a in soup.find_all("a", href=True)})
    interesting = [h for h in hrefs if re.search(r"prize|odd|rule|chance|pdf|structure|how-to", h, re.I)]
    print(" interesting links:", interesting[:20])
    # tables: count + header of each
    for ti, t in enumerate(soup.find_all("table")):
        rows = t.find_all("tr")
        hdr = [" ".join(c.get_text().split()) for c in (rows[0].find_all(["th","td"]) if rows else [])]
        print(f"  table[{ti}] {len(rows)} rows, header={hdr}")

for gid in IDS:
    try:
        dump(f"detail_{gid}", fetch.fetch_detail(gid))
    except Exception as e:
        print(f"detail {gid} FAILED: {e}")
    for tmpl in GUESS_URLS:
        url = tmpl.format(id=gid)
        try:
            dump(f"guess_{gid}_{tmpl.split('/')[-1].split('.')[0]}", fetch.fetch(url))
        except Exception as e:
            print(f"GUESS {url} -> {e}")
