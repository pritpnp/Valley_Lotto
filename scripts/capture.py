"""Fetch a game's DATA PDF (full prize structure: all tiers, original counts, odds)
and dump its text so we can confirm it has what we need for Jackpot Density /
weighted low-prize health."""
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bs4 import BeautifulSoup
from pdfminer.high_level import extract_text
from lottery_tracker import fetch

DEBUG = Path("debug"); DEBUG.mkdir(exist_ok=True)
BASE = "https://www.palottery.pa.gov"
IDS = ["3363", "3340"]  # Super 7s, Love Is Blind

for gid in IDS:
    html = fetch.fetch_detail(gid)
    soup = BeautifulSoup(html, "html.parser")
    pdfs = [a["href"] for a in soup.find_all("a", href=True) if a["href"].lower().endswith("_data.pdf")]
    if not pdfs:
        pdfs = [a["href"] for a in soup.find_all("a", href=True) if ".pdf" in a["href"].lower()]
    print(f"\n=== game id {gid}: pdf links = {pdfs} ===")
    for href in pdfs[:1]:
        url = href if href.startswith("http") else BASE + href
        name = href.split("/")[-1].replace(".pdf", "")
        try:
            r = fetch.fetch  # reuse session headers
            import requests
            from lottery_tracker.fetch import _HEADERS
            resp = requests.get(url, headers=_HEADERS, timeout=60); resp.raise_for_status()
            (DEBUG / f"{name}.pdf").write_bytes(resp.content)
            text = extract_text(DEBUG / f"{name}.pdf")
            (DEBUG / f"{name}.txt").write_text(text)
            print(f"  downloaded {len(resp.content)}b, {len(text)} chars")
            # show lines that look like a prize table (value, odds, counts)
            for ln in [l.strip() for l in text.splitlines() if l.strip()]:
                if re.search(r"\$[\d,]+|odds|1 in|total|prize|win", ln, re.I):
                    print("   |", ln[:120])
        except Exception as ex:
            print("  PDF fetch/parse failed:", ex)
