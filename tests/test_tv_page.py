"""The store TV page, run in a real browser with a fake clock and a fake
numbers feed (tools/tv_check.py): the 'Updated' note says when PA's numbers
were last read, says plainly when they're old, never misleads across
midnight, the page doesn't grow over hours offline, and the slide rotation
stays on course when new numbers arrive. Skipped where no browser is
installed."""

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_tv_page_behaves_over_time():
    pytest.importorskip("playwright.sync_api")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "tv_check.py"), str(ROOT / "tv")],
                       capture_output=True, text=True, timeout=900)
    if "Executable doesn't exist" in r.stderr or "BrowserType.launch" in r.stderr:
        pytest.skip("no Chromium for Playwright here")
    failed = [line for line in r.stdout.splitlines() if line.startswith("FAIL")]
    assert r.returncode == 0 and not failed, (failed or r.stdout[-2000:]) or r.stderr[-2000:]
