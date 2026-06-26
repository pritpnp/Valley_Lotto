# Valley Lotto 🎟️

A watchdog for **PA Lottery scratch-off** tickets, built for a retailer who wants
to stop losing money on dead inventory. Once a day it checks the official PA
Lottery lists and tells you when:

1. **A game ended** — sales stopped. If it's a game **you carry**, that's a
   red-alert: pull the dead stock and note the winner-redemption deadline.
2. **A game's prizes are too low** — the good prizes are mostly claimed, so it's
   time to swap that old game for a fresh one.
3. **A game you carry quietly disappeared** from the active list (usually means
   it ended).

It reads these three official pages:

| Page | What it gives us |
|------|------------------|
| [`ActivePrint`](https://www.palottery.pa.gov/scratch-offs/Print-Scratch-Offs.aspx?gametype=ActivePrint) | the authoritative list of games still on sale |
| [`Remaining`](https://www.palottery.pa.gov/scratch-offs/Print-Scratch-Offs.aspx?gametype=Remaining) | how many prizes are left per game |
| [`SalesEnded`](https://www.palottery.pa.gov/Scratch-Offs/Print-Scratch-Offs.aspx?gametype=SalesEnded) | games that ended + claim deadlines |

---

## 1. The only file you edit: `config.yaml`

```yaml
inventory:          # the games you carry, by PA game number
  - "5432"
  - "5310"

thresholds:
  top_prize_pct: 0.25        # "low" = under 25% of TOP prizes still unclaimed
  top_prize_count_floor: 1   # ...or 1-or-fewer top prizes left
  total_prize_pct: null      # optional rule on all prize tiers (e.g. 0.20)

report_all_games: false      # true = also scout EVERY game for low prizes
```

The **game number** is the 3–5 digit "Game #" printed on every PA scratch-off
page. Not sure of a number? Run the tracker once and open `reports/latest.md` —
every game it found is listed.

## 2. Run it

```bash
pip install -r requirements.txt
PYTHONPATH=src python -m lottery_tracker
```

Each run:
- prints a report and writes it to `reports/latest.md` (+ a dated copy),
- saves a snapshot to `data/state.json` (+ dated history) so the **next** run can
  detect what *changed*,
- writes `reports/alerts.json` (used by the GitHub Action), and
- exits `2` if there's a critical alert (a game you carry ended) — handy for CI.

> Alerts only fire on **changes**. You won't be re-pinged every day about the
> same game — only when something newly ends or newly crosses your threshold.

## 3. Automate it (recommended)

`.github/workflows/track.yml` runs the tracker **daily** on GitHub Actions,
commits the new snapshot, and **opens a GitHub Issue** whenever there's a new
alert. No servers, no setup beyond editing `config.yaml`. You can also trigger it
manually from the repo's **Actions** tab ("Run workflow").

### Optional: email alerts

The Action already wires up email — it switches on the moment you add these repo
**Settings → Secrets and variables → Actions** secrets:

| Secret | Example | Notes |
|--------|---------|-------|
| `SMTP_HOST` | `smtp.gmail.com` | |
| `SMTP_PORT` | `587` | |
| `SMTP_USER` | `you@gmail.com` | |
| `SMTP_PASS` | *app password* | For Gmail, create a Google **App Password**, not your login password |
| `ALERT_TO` | `you@gmail.com` | comma-separated for multiple recipients |

To test email locally, set those as environment variables and run the tracker.

## First real run — verify the columns

I built this in a sandbox that **couldn't reach `palottery.pa.gov`**, so the HTML
parser is written to be tolerant: it maps columns by their **header text** rather
than fixed positions. On your first live run, capture the pages and eyeball the
result:

```bash
PYTHONPATH=src python -m lottery_tracker --save-html   # saves pages to ./samples
```

If PA's column wording differs from what the parser expects, you'll get a clear
`ParseError` listing the headers it actually saw. Fixing it is usually a
one-liner: add the wording to the `_SYNONYMS` table in
[`src/lottery_tracker/parse.py`](src/lottery_tracker/parse.py). After that, you
can re-run offline against the saved pages with `--offline`.

## How it's organized

```
config.yaml                     # your games + thresholds  (the file you edit)
src/lottery_tracker/
  fetch.py    # download the 3 PA pages (browser UA; the pages 403 otherwise)
  parse.py    # HTML tables -> Game records, mapped by header name
  model.py    # the Game data model + merge of the 3 pages
  state.py    # save/load snapshots so we can diff runs
  rules.py    # the alert logic (ended / low-prizes / removed)
  notify.py   # Markdown report, alerts.json, optional email
  cli.py      # ties it together: fetch -> parse -> diff -> alert -> report
.github/workflows/track.yml     # daily schedule + auto-issue
tests/                          # offline tests with HTML fixtures
```

## Tests

```bash
pip install pytest
PYTHONPATH=src python -m pytest -q
```

The tests run fully offline against HTML fixtures in `tests/fixtures/`, so the
parsing and alert logic are verified without touching the network.
