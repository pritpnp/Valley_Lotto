"""Checks for the store TV page (tv/): the 'Updated' note and the slide rotation.

    python tools/tv_check.py tv [screenshot dir]

Run by tests/test_tv_page.py when Playwright and Chromium are installed.

Serves the copy at http://tv.test/ through Playwright routes, with a fake
/api/numbers whose answer each check controls, and a fake clock. Prints
PASS/FAIL per check and exits 1 if any check fails.
"""
import copy
import json
import mimetypes
import os
import sys
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

TV = os.path.abspath(sys.argv[1])
SHOTS = sys.argv[2] if len(sys.argv) > 2 else None
# Chromium: the one Playwright installed here if there is one, else Playwright's own.
_FOUND = sorted(__import__("glob").glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome"))
CHROME = _FOUND[-1] if _FOUND else None
SAMPLE = json.load(open(os.path.join(TV, "sample-numbers.json")))

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (" | " + detail if detail else ""))


def utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def feed(seen_at, generated_at):
    d = copy.deepcopy(SAMPLE)
    d["generated_at"] = generated_at
    for g in d["games"]:
        g["seen_at"] = seen_at
    return d


class Api:
    """What the fake /api/numbers answers: a dict, or None for a dropped connection."""
    def __init__(self):
        self.answer = None
        self.calls = 0


def open_page(p, api, start):
    browser = p.chromium.launch(executable_path=CHROME)
    ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, service_workers="block")

    def handle(route):
        url = route.request.url
        path = url.split("http://tv.test/", 1)[1].split("?", 1)[0] or "index.html"
        if api.answer is None and "alive=" in url:
            return route.abort()
        if path == "api/numbers":
            api.calls += 1
            if api.answer is None:
                return route.abort()
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(api.answer))
        f = os.path.join(TV, path)
        if not os.path.isfile(f):
            return route.fulfill(status=404, body="")
        ctype = mimetypes.guess_type(f)[0] or "application/octet-stream"
        if f.endswith(".woff2"):
            ctype = "font/woff2"
        return route.fulfill(status=200, content_type=ctype, body=open(f, "rb").read())

    ctx.route("http://tv.test/**", handle)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.clock.install(time=utc(start).timestamp() - 2)
    page.clock.pause_at(utc(start))
    page.goto("http://tv.test/")
    wait_until(page, "window.__tv && window.__tv.count() > 0")
    return browser, page, errors


def wait_until(page, js, ms=8000):
    for _ in range(ms // 50):
        if page.evaluate(js):
            return True
        page.wait_for_timeout(50)
    raise AssertionError("timed out waiting for: " + js)


def settle(page):
    # let a fetch the fake clock just started come back (real time)
    page.wait_for_timeout(400)


def note(page):
    return page.evaluate("document.getElementById('updated').textContent")


def note_fits(page):
    # the note stays inside the yellow label (it never runs onto the ticker)
    return page.evaluate("""(function () {
      var n = document.getElementById('updated');
      var r = document.createRange(); r.selectNodeContents(n);
      var t = r.getBoundingClientRect();
      var view = document.querySelector('.tk-view').getBoundingClientRect();
      return { left: Math.round(t.left), right: Math.round(t.right), viewLeft: Math.round(view.left), font: window.getComputedStyle(n).fontSize };
    })()""")


def run_for(page, ms):
    page.clock.run_for(ms)
    settle(page)


def current_slide(page):
    return page.evaluate("""(function () {
      var s = document.querySelector('#slides .slide.is-in');
      if (!s) return '';
      return Array.prototype.map.call(s.querySelectorAll('.half'), function (h) {
        var img = h.querySelector('.h-logo img');
        return img ? img.alt : h.querySelector('.wm').textContent;
      }).join(' + ');
    })()""")


def shot(page, name):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, name + ".png"), clip={"x": 0, "y": 940, "width": 640, "height": 140})


with sync_playwright() as p:
    # 1. The note says when the numbers were last read from PA (seen_at), not
    #    when the TV reached the app.
    api = Api()
    api.answer = feed("2026-10-29T13:55:00Z", "2026-10-29T14:00:00Z")   # 9:55 AM ET, asked at 10:00 AM
    browser, page, errors = open_page(p, api, "2026-10-29T14:00:00Z")
    settle(page)
    check("fresh numbers: note shows when PA was last read", note(page) == "Updated 9:55 AM", note(page))
    fit = note_fits(page)
    check("fresh note fits the label", fit["right"] <= fit["viewLeft"] - 16, str(fit))
    shot(page, "1-fresh")

    # The feed job stops: the app keeps answering (generated_at = now) with
    # the same old seen_at. Two hours on, the note says how old the numbers are.
    run_for(page, 60 * 60 * 1000)        # 11:00 AM
    api.answer = feed("2026-10-29T13:55:00Z", "2026-10-29T15:00:00Z")
    run_for(page, 2 * 60 * 1000)
    check("feed 1 hour behind: still 'Updated'", note(page) == "Updated 9:55 AM", note(page))
    api.answer = feed("2026-10-29T13:55:00Z", "2026-10-29T16:10:00Z")
    run_for(page, 70 * 60 * 1000)        # 12:12 PM
    check("feed 2+ hours behind: note says plainly how old", note(page) == "Numbers from 9:55 AM", note(page))
    cls = page.evaluate("document.getElementById('updated').className")
    check("stale note is marked (discreetly)", "is-stale" in cls, cls)
    fit = note_fits(page)
    check("stale note fits the label", fit["right"] <= fit["viewLeft"] - 16, str(fit))
    shot(page, "2-stale")
    # The feed job comes back: the note goes back to normal.
    api.answer = feed("2026-10-29T16:12:00Z", "2026-10-29T16:13:00Z")
    run_for(page, 2 * 60 * 1000 + 1000)
    check("feed back: note back to 'Updated'", note(page) == "Updated 12:12 PM" and "is-stale" not in page.evaluate("document.getElementById('updated').className"), note(page))
    check("no page errors", not errors, "; ".join(errors))
    browser.close()

    # 2. Across midnight during an outage the note gains the date, on its own.
    api = Api()
    api.answer = feed("2026-10-29T03:49:00Z", "2026-10-29T03:50:00Z")   # 11:49 PM ET Oct 28
    browser, page, errors = open_page(p, api, "2026-10-29T03:50:00Z")
    settle(page)
    check("11:50 PM online", note(page) == "Updated 11:49 PM", note(page))
    api.answer = None                    # internet drops
    run_for(page, 20 * 60 * 1000)        # 12:10 AM ET Oct 29
    check("12:10 AM offline: note gives the day", note(page) == "Updated Oct 28, 11:49 PM", note(page))
    fit = note_fits(page)
    check("dated note fits the label", fit["right"] <= fit["viewLeft"] - 16, str(fit))
    shot(page, "3-after-midnight")
    run_for(page, 2 * 60 * 60 * 1000)    # 2:10 AM: more than two hours old
    check("2:10 AM offline: note says the numbers are old", note(page) == "Numbers from Oct 28, 11:49 PM", note(page))
    fit = note_fits(page)
    check("longest note fits the label", fit["right"] <= fit["viewLeft"] - 16, str(fit))
    shot(page, "4-stale-dated")
    run_for(page, 8 * 60 * 60 * 1000)    # 10:10 AM
    check("10:10 AM offline", note(page) == "Numbers from Oct 28, 11:49 PM", note(page))
    # no memory growth: a long outage keeps the page the same size
    n1 = page.evaluate("document.getElementsByTagName('*').length")
    run_for(page, 12 * 60 * 60 * 1000)
    n2 = page.evaluate("document.getElementsByTagName('*').length")
    check("no growth over 12 more hours offline", abs(n2 - n1) < 400, "%d -> %d nodes" % (n1, n2))
    check("no page errors", not errors, "; ".join(errors))
    browser.close()

    # 3. New data during the repeated hero slide must not skip PICK 2-5.
    api = Api()
    api.answer = feed("2026-10-29T14:00:00Z", "2026-10-29T14:00:30Z")
    browser, page, errors = open_page(p, api, "2026-10-29T14:00:30Z")
    settle(page)
    plan = page.evaluate("__tv.plan()")
    heroes = [i for i, s in enumerate(plan) if s == plan[0]]
    print("plan:", plan)
    check("hero pair comes back in the cycle", len(heroes) >= 2, str(heroes))
    again = heroes[1]
    expected = plan[again + 1]
    # the first poll answered at start; the next one is due 2 minutes later
    run_for(page, 2 * 60 * 1000 - 5000)
    page.evaluate("__tv.go(%d)" % again)
    settle(page)
    check("on the repeated hero slide", current_slide(page) == plan[again], current_slide(page))
    api.answer = feed("2026-10-29T14:10:00Z", "2026-10-29T14:02:25Z")   # only seen_at changes
    calls = api.calls
    run_for(page, 6000)                  # the poll fires while the hero slide is up
    check("new data arrived during the hero slide", api.calls > calls and current_slide(page) == plan[again], current_slide(page))
    run_for(page, 21000)                 # a hero slide holds 21 s
    nxt = current_slide(page)
    check("next slide after the repeated hero is " + expected, nxt == expected, nxt)
    # and the cycle carries on in order from there
    run_for(page, 17000)                 # other slides hold 17 s
    check("then " + plan[again + 2], current_slide(page) == plan[again + 2], current_slide(page))

    # Same, during the first hero slide: carries on with slide 1. The last
    # poll answered 2:02:31 (fake clock), so the next one is due at 2:04:31.
    api.answer = feed("2026-10-29T14:20:00Z", "2026-10-29T14:04:25Z")
    run_for(page, 77000)                 # 2:04:26
    page.evaluate("__tv.go(0)")
    settle(page)
    calls = api.calls
    run_for(page, 6000)
    check("new data arrived during the first hero slide", api.calls > calls and current_slide(page) == plan[0], current_slide(page))
    run_for(page, 21000)
    check("next slide after the first hero is " + plan[1], current_slide(page) == plan[1], current_slide(page))
    check("no page errors", not errors, "; ".join(errors))
    browser.close()

print("%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
