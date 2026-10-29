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
  "fixed" note when it clears.
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
`migrate.enable_rls(engine)`, so they're never left exposed to Supabase's API.
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
    mean none.
  * The feed's BOM / XML declaration must be handled (the real file starts with
    a UTF-8 BOM). Use `xml.etree.ElementTree` (or defusedxml if available); no
    regex parsing of XML.
  * `parse_old_scraper(json_text) -> list[Draw]` for `lottery.json`
    (`megaMillions`/`powerball`: `numbers` = 5 + bonus, `date` "October 27,
    2026 Drawing", `nextDrawing` "Friday, October 30, 2026", `nextJackpot`
    "$463 Million"). Map to game ids 15 (Mega Millions, bonus code MB) and 12
    (Powerball, bonus code PB). Bonus value zero-padded to 2 digits like PA.
    "$463 Million" → 463000000 (strict: number + Million/Billion only).
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
  * Mismatch: Telegram immediately with both readings, reset the trial.
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
  `numbers` = fetch feed + fetch old `lottery.json` from
  `https://raw.githubusercontent.com/pritpnp/powerball-megamillion/main/lottery.json`
  + save + compare + maybe switch. `orders` = retailers page → link → PDF
  (download only if the URL/date is new) → parse → save → Telegram the change
  (first list: count only; later lists: games added and dropped, with names
  and prices).
* Workflow `.github/workflows/pa-feeds.yml`: `*/10 * * * *` runs `numbers`;
  `7 * * * *` (hourly) runs `orders`; `workflow_dispatch` runs `all`.
  `concurrency: pa-feeds`. Secrets: `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`. Installs `requirements.txt` + `requirements-app.txt`
  (SQLAlchemy, psycopg) + `requirements-feeds.txt` (pdfplumber). A failed run
  must not email-storm: the job itself reports problems via Telegram with
  de-duplication, and exits 0 for known data problems; it exits non-zero only
  for real crashes (bugs).

## TV feed: `GET /api/numbers` (the app, no login)

Public data. Headers: `Access-Control-Allow-Origin: *`,
`Cache-Control: public, max-age=60`. Never includes store data.

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
  cash value) are added only when PA's reading of that same draw matched.
  After the switch: PA's feed only.

## TV page: `tv/` (Netlify; shown by AbleSign)

* Static site, no build tools needed: `tv/index.html`, `tv/app.js`,
  `tv/styles.css`, self-hosted fonts under an open licence (OFL; include the
  licence file). No external network requests except `/api/numbers`.
* `tv/netlify.toml`: publish `.`; a build command that writes `_redirects`
  proxying `/api/numbers` → `$APP_URL/api/numbers` (200, forced) from the
  `APP_URL` Netlify environment variable, failing the build with a plain
  message if it's missing. Also a `[build] ignore` so Netlify only rebuilds
  when `tv/` changes. Allow `?api=<url>` to override for testing.
* 1920×1080 stage, scaled to fit any screen (also works at 1280×720 and 4K),
  letterboxed, no scrollbars, cursor hidden, nothing to click.
* Cycles through every game in the feed, all 16 today. Grouping a game's Day
  and Evening draws onto one slide is fine as long as every draw is shown.
  Each slide: game name (styled text, NO PA or Powerball/Mega Millions logos),
  draw date, the numbers as balls (bonus balls styled distinctly and labelled),
  next draw ("TONIGHT" when it's today in America/New_York), jackpot (big),
  cash value when present. Long jackpot texts ("$1,000,000 A Year For Life")
  must fit.
* Data: fetch `/api/numbers` every 2 minutes; keep the last good data in
  localStorage and show it at once on load; if a fetch fails, keep showing the
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
