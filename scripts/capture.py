"""Follow each game's PA Bulletin link (pacodeandbulletin.gov) and dump the
official prize table — every prize level's total winners (original count) + odds."""
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bs4 import BeautifulSoup
from lottery_tracker import fetch

DEBUG = Path("debug"); DEBUG.mkdir(exist_ok=True)
IDS = ["3363", "3340"]

for gid in IDS:
    html = fetch.fetch_detail(gid)
    soup = BeautifulSoup(html, "html.parser")
    bull = [a["href"] for a in soup.find_all("a", href=True) if "pacodeandbulletin.gov" in a["href"]]
    print(f"\n=== game id {gid}: bulletin = {bull} ===")
    for url in bull[:1]:
        try:
            bhtml = fetch.fetch(url)
            (DEBUG / f"bulletin_{gid}.html").write_text(bhtml)
            bsoup = BeautifulSoup(bhtml, "html.parser")
            text = bsoup.get_text("\n")
            (DEBUG / f"bulletin_{gid}.txt").write_text(text)
            print(f"  fetched {len(bhtml)}b")
            # print lines around prize/winners/odds
            for ln in [l.strip() for l in text.splitlines() if l.strip()]:
                if re.search(r"\$[\d,]+|winner|odds|1 in|ticket|approximate", ln, re.I):
                    print("   |", ln[:140])
        except Exception as ex:
            print("  bulletin fetch failed:", ex)
