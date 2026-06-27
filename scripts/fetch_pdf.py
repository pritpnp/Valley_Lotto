"""One-off: download a PA Lottery PDF and extract its text to debug/ for reading."""
import sys
from pathlib import Path
import requests
from pdfminer.high_level import extract_text

URL = sys.argv[1] if len(sys.argv) > 1 else (
    "https://www.palottery.pa.gov/PaLotteryWebSite/media/Retailer-Documents/News/SalesMaker_Summer2026.pdf"
)
HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
DEBUG = Path("debug"); DEBUG.mkdir(exist_ok=True)
r = requests.get(URL, headers=HDRS, timeout=60); r.raise_for_status()
(DEBUG / "salesmaker.pdf").write_bytes(r.content)
text = extract_text(DEBUG / "salesmaker.pdf")
(DEBUG / "salesmaker.txt").write_text(text)
print(f"downloaded {len(r.content)} bytes, extracted {len(text)} chars")
