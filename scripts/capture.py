"""Diagnostic: fetch the live PA pages, dump raw HTML to debug/, and print the
structure of every table (headers + first data row) so we can map columns
correctly. Runs on a GitHub runner (which can reach palottery.pa.gov).

This is a developer tool, not part of the daily tracker.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bs4 import BeautifulSoup  # noqa: E402

from lottery_tracker import fetch  # noqa: E402

DEBUG = Path(__file__).resolve().parents[1] / "debug"
DEBUG.mkdir(exist_ok=True)

PAGES = {
    "active": fetch.fetch_active,
    "remaining": fetch.fetch_remaining,
    "sales_ended": fetch.fetch_sales_ended,
}


def clean(s: str) -> str:
    return " ".join((s or "").split())


def summarize(name: str, html: str) -> None:
    (DEBUG / f"{name}.html").write_text(html)
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    print(f"\n{'='*70}\nPAGE: {name}  ({len(html)} bytes, {len(tables)} tables)\n{'='*70}")
    for ti, table in enumerate(tables[:8]):
        rows = table.find_all("tr")
        print(f"\n  -- table[{ti}]: {len(rows)} rows --")
        for ri, row in enumerate(rows[:3]):
            cells = [clean(c.get_text()) for c in row.find_all(["th", "td"])]
            print(f"     row[{ri}] ({len(cells)} cells): {cells}")


DETAIL_URL = "https://www.palottery.pa.gov/Scratch-Offs/View-Scratch-Off.aspx?id={id}"


def capture_detail(detail_id: str) -> None:
    html = fetch.fetch(DETAIL_URL.format(id=detail_id))
    (DEBUG / f"detail_{detail_id}.html").write_text(html)
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    print(f"\n{'='*70}\nDETAIL id={detail_id}  ({len(html)} bytes, {len(tables)} tables)\n{'='*70}")
    # Dump EVERY table fully to find any complete prize-tier breakdown.
    for ti, table in enumerate(tables):
        rows = table.find_all("tr")
        print(f"\n  -- table[{ti}]: {len(rows)} rows --")
        for ri, row in enumerate(rows):
            cells = [clean(c.get_text()) for c in row.find_all(["th", "td"])]
            print(f"     row[{ri}] ({len(cells)} cells): {cells}")
    # Save full visible text and surface lines about tickets / odds / per-tier data.
    text = soup.get_text("\n")
    (DEBUG / f"detail_{detail_id}.txt").write_text(text)
    import re as _re
    keep = [ln.strip() for ln in text.split("\n")
            if ln.strip() and _re.search(r"odds|ticket|1 in|1:|chance|prize|remaining|sold|total|%", ln, _re.I)]
    print("\n  -- text lines of interest --")
    for ln in keep[:80]:
        print("    |", ln[:120])


def main() -> None:
    for name, fn in PAGES.items():
        try:
            html = fn()
            summarize(name, html)
        except Exception as e:  # noqa: BLE001
            print(f"\nPAGE: {name} -> FETCH FAILED: {e}")
    # Capture a couple of detail pages to learn the original-prize-count layout.
    for detail_id in ("3363", "3340"):
        try:
            capture_detail(detail_id)
        except Exception as e:  # noqa: BLE001
            print(f"\nDETAIL {detail_id} -> FAILED: {e}")


if __name__ == "__main__":
    main()
