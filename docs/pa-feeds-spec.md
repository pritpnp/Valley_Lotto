# PA feeds: winning numbers for the store TVs, and the weekly order list

Two things Pennsylvania publishes, read on a schedule, saved in the store's
Supabase database, and used by the app and the store TVs.

* **Winning numbers**: `https://www.palottery.pa.gov/game-number-feed.aspx`
  (XML, every draw game). Shown on the store TVs (AbleSign signage, 1920×1080
  landscape) from a page hosted on Netlify.
* **Scratch-offs available for ordering**: a PDF linked from
  `https://www.palottery.pa.gov/About-PA-Lottery/For-Retailers/Current-Retailers.aspx`
  as "Scratch-Offs Available for Ordering". The link changes every week. Saved
  once per "As of" date. Used by the app: real tickets-per-pack, only suggest
  games you can order, flag carried games PA no longer ships, Telegram when the
  list changes.

Powerball and Mega Millions are also scraped by the old project
(`pritpnp/powerball-megamillion`, writes `lottery.json` daily at ~1am ET). Both
run side by side; every draw is compared; after 14 days of exact matches the
TVs switch to PA's feed for those two games and the old scraper turns itself off.

Real samples of all three inputs are in `tests/fixtures/pa/` (fetched
2026-10-28). The sandbox can't reach palottery.pa.gov; GitHub Actions can.

## Rules that apply everywhere (same as the rest of this repo)

* Never guess. A value that can't be read exactly is left out and the reason is
  recorded. Money is parsed strictly (`$716,000,000` → 716000000;
  `$1,000,000 A Year For Life` → amount `None`, text kept).
* A format change must fail loudly (Telegram, once per distinct problem), never
  produce wrong numbers quietly. Parse by content (headings, labels), not by
  position.
* Telegram: HTML parse mode, escape PA text, never log the token. Reuse
  `lottery_tracker.telegram.send`. Never send the same problem twice in a row:
  remember the last problem's signature in `pa_feed_settings`; send a short
  "fixed" note when it clears. What a message is about (and that it was sent)
  is saved, committed, before the message goes: a database that won't save it
  stops the message too, and nothing that fails later in the run can have it
  sent again every ten minutes. A message Telegram doesn't take is tried again
  at the next run: a problem's signature is put back; a one-off message (a
  mismatch, the switch, a new order list) waits in `pa_feed_settings`
  (`pending:<event>`) until it's delivered, and only the message is tried
  again, never what it's about.
* The repo is PUBLIC. No secrets, no store data in the repo. PA's public pages
  as test fixtures are fine.
* Plain language in anything the user reads (he's a store owner, not a coder).

## Database (already defined in `src/lottery_tracker/web/models.py`)

`DrawResultRow` (`pa_draw_results`), `DrawCheckRow` (`pa_draw_checks`),
`FeedSettingRow` (`pa_feed_settings`), `OrderListRow` (`pa_order_lists`),
`OrderGameRow` (`pa_order_games`). The app creates them on boot
(`ensure_schema`) and turns on row-level security for every table
(`migrate.enable_rls`). The feed job must also create *only these* tables if
missing (`Base.metadata.create_all(engine, tables=[...])`) and then call
`migrate.enable_rls(engine, tables=[...the same five...])`, so they're never
left exposed to Supabase's API. `enable_rls` only alters a table whose RLS is
still off, waiting at most a few seconds for its lock: `ALTER TABLE` locks the
whole table, and the job runs every ten minutes beside the live app.
Postgres in production (Supabase, via `DATABASE_URL`), SQLite in tests.

## Package: `src/lottery_tracker/pa_feeds/`

* `draws.py`
  * `parse_feed(xml_text) -> list[Draw]`. Each `<game id>` → `Draw(game_id,
    title, draw_date ISO, numbers: list[str] as published (keep leading zeros),
    extras: list[{"code","label","value"}], jackpot_date ISO|None,
    jackpot_text|None, jackpot_amount float|None, cash_value_text|None,
    cash_value_amount float|None, raw: the <game> element text)`. Keep the
    feed's order (`position`).
  * `<numbers>` format: main number tokens, then zero or more `LABEL: value`
    pairs where LABEL may contain spaces (`WB: 7`, `PB: 01 PP: 03`, `MB: 03`,
    `Mill Ball: 03`). Known labels → readable names: WB = Wild Ball,
    PB = Powerball, PP = Power Play, MB = Mega Ball, Mill Ball = Millionaire
    Ball. An unknown label is kept (label = the code) and reported once as a
    format change. Dates are `MM/DD/YYYY`; empty elements (`<jackpotdate />`)
    mean none. A next draw date (`jackpotdate`) is kept only if it's after the
    draw date and at most 7 days after it (Powerball Xs & Os draws weekly);
    otherwise it's left out and reported. The one exception is the draw's own
    date (PA has posted the numbers but not yet moved the next draw on): it's
    left out without a report, since the feed is read every ten minutes and
    that would be a message at every draw.
  * The feed's BOM / XML declaration must be handled (the real file starts with
    a UTF-8 BOM). Use `xml.etree.ElementTree` (or defusedxml if available); no
    regex parsing of XML.
  * `parse_old_scraper(json_text) -> list[Draw]` for `lottery.json`
    (`megaMillions`/`powerball`: `numbers` = 5 + bonus, `date` "October 27,
    2026 Drawing", `nextDrawing` "Friday, October 30, 2026", `nextJackpot`
    "$463 Million"). Map to game ids 15 (Mega Millions, bonus code MB) and 12
    (Powerball, bonus code PB). Bonus value zero-padded to 2 digits like PA.
    "$463 Million" → 463000000 (strict: number + Million/Billion only). A
    `nextJackpot` that isn't an amount at all (the old scraper writes
    "Unavailable" when powerball.com has no jackpot line) is left out and
    reported; one that starts with a `$` amount keeps its text. `nextDrawing`
    must name the right weekday, and is kept only if it's after the draw and
    at most 7 days after it, as for PA's `jackpotdate` (the old scraper writes
    the year that's ending into a January 1 next draw); otherwise it's left out
    and reported.
* `orders.py`
  * `find_order_link(html, base_url) -> url`: the `<a>` whose text, with
    `&nbsp;`/whitespace collapsed, is "Scratch-Offs Available for Ordering"
    (case-insensitive). Resolve relative hrefs. None found → error.
  * `parse_order_pdf(pdf_bytes) -> OrderList(as_of ISO, games: list[OrderGame
    (game_number, price, tickets_per_pack, name)])` with pdfplumber. Find the
    header by content (`Price`, `Tix/Pack`, `GAMES AVAILABLE TO ORDER`, and
    "As of" followed by a date `MM/DD/YY`). Every row must be exactly
    game number (digits) / `$price` / integer / name, or the whole list is
    rejected. Zero rows → rejected.
  * Cross-check (warnings, not rejections): the URL's date (`..._10-26-26.pdf`)
    vs the PDF's "As of"; prices vs `data/state.json`; games on the list that
    `state.json` doesn't know.
* `store.py`: DB work. Upsert draws by (source, game_id, draw_date): insert
  once, then update `last_seen_at` and the jackpot fields (estimates move).
  Remember the feed's game order in settings (`feed_order`). Save an order
  list only if its `as_of` isn't saved yet; if the same `as_of` comes back
  with a different PDF hash, save nothing and report it once.
* Comparison and switch (Powerball 12, Mega Millions 15):
  * For each draw date present in BOTH sources, compare main numbers (as
    integers, in order) and the bonus ball (as an integer). Jackpot and next
    draw date are compared and noted in `detail`, but do not decide `matched`
    (the two are read at different times of day and estimates change).
  * Settings: `pbmm_source` = `old_scraper` until switched, then `pa_feed`;
    `trial_started_at` (first comparison; reset to the time of any mismatch);
    `switched_at`.
  * Switch when ALL hold: ≥ 14 days since `trial_started_at`; every comparison
    since then matched; ≥ 4 matched draws of each game since then. On switch:
    Telegram "✅ ..." once. After switching, keep comparing if old data still
    arrives; a mismatch is reported but never switches back automatically.
  * Mismatch: Telegram immediately with both readings, reset the trial. (A
    message Telegram doesn't take goes out at a later run; the trial isn't
    reset again for it.)
  * Old scraper data missing for > 3 days during the trial: report once.
* `display.py`: `build_payload(session) -> dict` (the TV feed, below). Used by
  the app's `/api/numbers`.
* `__main__.py`: `python -m lottery_tracker.pa_feeds numbers|orders|all`
  (`--dry-run` parses and prints, no DB, no Telegram). Reads
  `DATABASE_URL`; if it isn't set, print one plain sentence saying the
  database link must be added as a GitHub secret, and exit 0 (no failure
  emails every ten minutes). Network fetches use a browser User-Agent,
  timeouts, and one retry. A fetch that fails is reported only after it has
  kept failing for over an hour (remember first failure time).
  A problem with the database itself is a `::warning::` and exit 0, never a
  failed run: it can't be reached, it turns down the login (an old password in
  the GitHub secret after a Supabase password reset), it won't save anything
  (Supabase's read-only mode, Postgres code 25006, when a project is over its
  size limit), or `DATABASE_URL` isn't a usable link. Nothing can be remembered
  in a database like that, so the owner is told on Telegram by a stateless
  rule: only on the `numbers` run that starts between 9:00 and 9:10 AM
  America/New_York, about once a day while it lasts. The message says what to
  do (put the same link Render uses into the GitHub secret `DATABASE_URL`;
  for read-only, free up space or upgrade the Supabase plan). Never the link
  or any part of it, in the log or on Telegram. (The keepalive check can't
  see these: it checks the app, which has its own copy of the link.)
  `numbers` = fetch feed + fetch old `lottery.json` from
  `https://raw.githubusercontent.com/pritpnp/powerball-megamillion/main/lottery.json`
  + save + compare + maybe switch. `orders` = retailers page → link → PDF
  (download only if the URL/date is new) → parse → save → Telegram the change
  (first list: count only; later lists: games added and dropped, with names
  and prices).
* Workflow `.github/workflows/pa-feeds.yml`: `4,14,24,34,44,54 * * * *` runs
  `numbers`; `37 * * * *` (hourly) runs `orders`; `workflow_dispatch` runs
  `all`. Off the top of the hour, where GitHub drops the most scheduled runs.
  (The numbers don't depend on it: see "Where fresh numbers come from".)
  `concurrency: pa-feeds`. Secrets: `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`. Installs `requirements.txt` + `requirements-app.txt`
  (SQLAlchemy, psycopg) + `requirements-feeds.txt` (pdfplumber). A failed run
  must not email-storm: the job itself reports problems via Telegram with
  de-duplication, and exits 0 for known data problems; it exits non-zero only
  for real crashes (bugs).

## Where fresh numbers come from, and why

GitHub's schedule is best-effort. On 2026-10-29 it ran the "every 10 minutes"
numbers job 6 times in 18 hours (gaps of 1 to 3 hours), so after a draw the
TVs could show numbers hours old. So the app reads PA's numbers itself
(`pa_feeds/in_app.py`):

* **When.** Each `/api/numbers` request makes one small query (the settings
  `numbers_last_attempt_at` and `numbers_last_run_at`, written by every
  numbers run, the app's and GitHub's, at its start and at its end). If
  neither is within the last 5 minutes, a numbers run starts in a background
  thread with its own database session, and the request is answered at once
  with what's saved: a TV never waits for PA. A run that fails still wrote its
  start, so a PA site that's down is tried again every 5 minutes, not at every
  TV's check. One that failed before it could write it (Supabase gone
  read-only, no connection to be had) is held back the same 5 minutes by the
  app itself. PA's numbers are saved before the old scraper's file is asked
  for, so a slow old-scraper site never holds the database for writing (on
  SQLite that would hold up a clerk's save).
* **Who asks.** The TVs (every 2 minutes, through Netlify), and Netlify's
  scheduled function `tv/netlify/functions/refresh-numbers.mts` every 10
  minutes, which runs on time. That also keeps Render's free plan from putting
  the app to sleep (it sleeps after 15 minutes without a visitor). So PA is
  read about every 5 to 10 minutes, day and night, TVs on or off.
* **One run at a time.** In the app, a lock in memory (gunicorn runs one
  worker). Across the app and the GitHub job (and two copies of the app while
  Render swaps in a new one), a Postgres advisory lock (`store.numbers_lock`):
  a run that finds it taken skips its numbers quietly, since the other run is
  reading the same feeds. The lock is a transaction lock
  (`pg_try_advisory_xact_lock`) held by a transaction kept open on a
  connection of its own for the whole run, then ended in `finally`: through
  Supabase's pooler in transaction mode, a session lock's unlock can land on a
  different database connection and leave the lock held for good. Nothing to
  lock on SQLite (tests).
* **Quiet.** Render has no Telegram settings, so a run in the app never sends a
  message, or drops one: every message it has (a mismatch, the switch, a
  problem or its "fixed" note, "turn the old scraper off/on") goes on the
  waiting list (`pending:...` in `pa_feed_settings`), saved together with what
  it's about and in the order it was said. The next GitHub run delivers the
  list first, once. The side-by-side check, the trial and the switch work as
  in a GitHub run: each draw is compared once, and saved with its message. The
  daily "the database has a problem" message is the GitHub job's alone.
* **Turned off** with `PA_FEED_IN_APP=0` on Render (then only the GitHub job
  reads PA). Off in the tests unless a test turns it on.

GitHub's runs go on as before (now at 4, 14, 24... past the hour): they read
the numbers too, as a backup, and deliver what's waiting. A Telegram message
the app queued goes out at the next GitHub run that starts, which is usually
within the hour but can be a few hours on GitHub's bad days.

## TV feed: `GET /api/numbers` (the app, no login)

Public data. Headers: `Access-Control-Allow-Origin: *`,
`Cache-Control: public, max-age=60`. Never includes store data, and never
sends a session cookie, not even to a visitor who is signed in to the app
(the app's session interface skips saving the session for public feeds; a
shared cache could otherwise hand one visitor's login to another).

```json
{
  "generated_at": "2026-10-29T00:20:00Z",
  "pbmm_source": "old_scraper",
  "games": [
    {
      "id": "12",
      "title": "Powerball",
      "position": 11,
      "draw_date": "2026-10-26",
      "numbers": ["18", "26", "49", "50", "54"],
      "extras": [{"code": "PB", "label": "Powerball", "value": "01"},
                 {"code": "PP", "label": "Power Play", "value": "03"}],
      "next_draw_date": "2026-10-28",
      "jackpot": {"text": "$716,000,000", "amount": 716000000},
      "cash_value": {"text": "$307,200,000", "amount": 307200000},
      "source": "pa_feed",
      "seen_at": "2026-10-29T00:10:00Z"
    }
  ]
}
```

* One entry per game id, its latest draw, in the feed's order (`position`).
  All 16 games in the feed today: PICK 2/3/4/5 (Day) and (Evening), Treasure
  Hunt, Cash 5, Match 6, Powerball, Mega Millions, Millionaire for Life,
  Powerball Double Play, Powerball Xs & Os. Any new game PA adds appears
  automatically.
* `next_draw_date` = the feed's `jackpotdate` (PA lists the jackpot for the
  next draw). `jackpot`/`cash_value` are `null` when empty.
* Powerball (12) and Mega Millions (15) while `pbmm_source` is `old_scraper`:
  numbers, bonus ball, draw date, next draw and jackpot come from the old
  scraper's latest draw. PA's extras that the old scraper lacks (Power Play,
  cash value, and the jackpot when it had none) are added only when PA's
  reading of that same draw matched.
  After the switch: PA's feed only.

## TV page: `tv/` (Netlify; shown by AbleSign)

* Static site, no build tools needed: `tv/index.html`, `tv/app.js`,
  `tv/styles.css`, self-hosted fonts under an open licence (OFL; include the
  licence file). No external network requests except `/api/numbers`.
* `tv/netlify.toml`: publish `.`; a build command that writes `_redirects`
  proxying `/api/numbers` → `$APP_URL/api/numbers` (200, forced) from the
  `APP_URL` Netlify environment variable, failing the build with a plain
  message if it's missing. Also a `[build] ignore` so Netlify only rebuilds
  when `tv/` or `APP_URL` changes (the build publishes a fingerprint of
  `APP_URL`, `app-url.sha256`, which the next deploy compares). Allow
  `?api=<url>` to override for testing.
* 1920×1080 stage, scaled to fit any screen (also works at 1280×720 and 4K),
  letterboxed, no scrollbars, cursor hidden, nothing to click.
* Cycles through every game in the feed, all 16 today. Grouping a game's Day
  and Evening draws onto one slide is fine as long as every draw is shown.
  Each slide: the game's official logo as PA shows it on palottery.pa.gov
  (`tv/logos/`; Double Play uses Powerball's with a "DOUBLE PLAY" label; a game
  PA adds later gets a styled name until it has one); the ticket price ("$2 Ticket") when known; "WINNING NUMBERS"; the
  draw date; the numbers as balls (bonus balls in the game's own colour and
  labelled); "Next Drawing:" with "TONIGHT" when it's today in
  America/New_York; the jackpot in a loud point-out box; cash value when
  present. Long jackpot texts ("$1,000,000 A Year For Life") must fit.
* Look and feel (owner's direction, from his current sign): bold retail
  point-of-sale energy, NOT a dry or generic "AI" look. Each game in its own
  saturated brand colour block (Mega Millions blue #234090, Powerball red
  #ab1e2c on his sign), heavy white uppercase type, a dark band carrying big
  number balls, a yellow "$X Ticket" callout, and the jackpot as the big
  point-out (his is green #197b30 with a yellow border). Keep that punch and
  familiarity, make it polished and animated, and fit more games than his
  two-up sign.
* Ticket prices come from `tv/games.json` (per game id: display name, colour,
  logo, price text). From PA's own Game Guide (dated 2026-04-07) and game
  pages (read 2026-10-28):

  | Game (feed id) | Price shown |
  |---|---|
  | Powerball (12) | $2 (Power Play +$1) |
  | Mega Millions (15) | $5 |
  | Millionaire for Life (37) | $5 |
  | Cash 5 (8) | $2 (includes Quick Cash) |
  | Match 6 (11) | $2 |
  | Treasure Hunt (7) | $1 |
  | PICK 2 (31, 27) | $1 |
  | PICK 3 (32, 28) | 50¢ or $1 |
  | PICK 4 (33, 29) | 50¢ or $1 |
  | PICK 5 (34, 30) | $1 |
  | Powerball Double Play (36) | +$1 add-on to Powerball |
  | Powerball Xs & Os (38) | $5 |

  Wild Ball doubles the cost of a PICK play. A game PA adds later has no
  price until one is put in `games.json`, and shows none.
* Data: fetch `/api/numbers` every 2 minutes; keep the last good data in
  localStorage and show it at once on load (a small service worker, `sw.js`,
  keeps a copy of the page's own files, fresh from the network first, so the
  page also opens with no internet after a power cut; it never keeps
  `/api/numbers`); if a fetch fails, keep showing the
  last data and retry; a small, discreet "last updated" note. **Reload the
  whole page once an hour no matter what** (so the screen always ends up
  current even if the script stalls), keeping the cached data on screen
  through the reload. Never show made-up numbers: with no data at all, show a
  calm "Today's numbers are on their way" screen. A `?demo=1` mode may load the
  real fixture sample for previews only, clearly not the default.
* Signage-safe: runs on older Chromium WebViews (AbleSign players on Android /
  Fire TV sticks): plain ES2017 JS, no frameworks, no CSS nesting / `:has` /
  container queries. Animate only `transform` and `opacity`; no heavy blur or
  large canvases; aim for smooth 60fps on a cheap stick and low CPU when idle.
  Must run for days without leaking memory (no growing DOM, timers cleared).
* Professional custom animated background and transitions.

## Telling the owner to turn the old scraper on or off (Telegram)

This session can't change the old repo, so the owner turns its workflow on or
off by hand; the feed job tells him when, on the existing Telegram bot.

* **Is the old scraper running?** Inferred from its data, nothing else: it is
  "running" if its `lottery.json` has shown a draw date newer than any it showed
  before within the last 4 days (Powerball draws Mon/Wed/Sat and Mega Millions
  Tue/Fri, so a daily scraper always has something new within 4 days).
  Remember the last time new old-scraper data appeared (`old_last_new_at`).
* **Turn it OFF**: once switched (`pbmm_source` = `pa_feed`) while the old
  scraper still looks like it's running, send "🔌 Turn the old Powerball/Mega
  Millions scraper OFF" with the exact steps: GitHub → powerball-megamillion →
  Actions → "Daily Lottery Update" → ⋯ (top right) → Disable workflow. Remind
  at most once a week while it still looks like it's running.
* **Turn it back ON**: after the switch, if PA's feed stops being trustworthy
  for Powerball or Mega Millions, send "⚠️ Turn the old scraper back ON" with
  the steps (same place → Enable workflow), saying why. Triggers: PA's newest
  draw for game 12 or 15 is more than 4 days old (draws missed), the PA feed
  has been failing for more than 6 hours, or a mismatch with old data (if any
  still arrives). Once per distinct problem; a "PA's feed is fine again, you can
  turn the old scraper OFF again" note when it clears.
* **TV fallback**: after the switch, if the old scraper has a NEWER draw for
  game 12 or 15 than PA's feed (PA stale, owner turned the old one back on),
  the TV shows the old scraper's draw for that game until PA catches up.
