"""Download the two PA Lottery print pages.

These pages reject non-browser user agents (you'll get a 403 otherwise), so we
send a normal desktop browser UA. Network access is required at runtime — the
pages are *not* reachable from every sandbox, so failures are reported clearly.
"""

from __future__ import annotations

import time

import requests

# The "print" variants of the scratch-off pages are plain server-rendered HTML
# tables, which is far easier to parse than the JS-driven public pages.
SALES_ENDED_URL = (
    "https://www.palottery.pa.gov/Scratch-Offs/Print-Scratch-Offs.aspx?gametype=SalesEnded"
)
REMAINING_URL = (
    "https://www.palottery.pa.gov/Scratch-Offs/Print-Scratch-Offs.aspx?gametype=Remaining"
)
# Authoritative list of games still on sale right now. We treat "active = appears
# here" and infer an ending the moment a game we carry drops off this list.
ACTIVE_URL = (
    "https://www.palottery.pa.gov/Scratch-Offs/Print-Scratch-Offs.aspx?gametype=ActivePrint"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class FetchError(RuntimeError):
    pass


def fetch(url: str, *, retries: int = 3, timeout: int = 30) -> str:
    """GET ``url`` and return the HTML text, retrying transient failures."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=timeout)
            resp.raise_for_status()
            if not resp.text.strip():
                raise FetchError(f"Empty response body from {url}")
            return resp.text
        except Exception as e:  # noqa: BLE001 — we re-raise a wrapped error below
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s
    raise FetchError(f"Failed to fetch {url} after {retries} attempts: {last_err}")


def fetch_sales_ended(**kw) -> str:
    return fetch(SALES_ENDED_URL, **kw)


def fetch_remaining(**kw) -> str:
    return fetch(REMAINING_URL, **kw)


def fetch_active(**kw) -> str:
    return fetch(ACTIVE_URL, **kw)
