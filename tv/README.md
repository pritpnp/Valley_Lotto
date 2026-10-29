# Store TV: PA winning numbers

This folder is the web page your store TVs show. AbleSign opens it full screen
and leaves it running. It shows the latest Pennsylvania Lottery winning numbers
for every draw game, two games at a time in their own colours, with the jackpot
in the big green box and a ticker along the bottom with every daily draw.

The game logos are PA's official game logos, as palottery.pa.gov shows them.

## 1. Put it on Netlify (one time)

1. Sign in at netlify.com and choose **Add new site > Import an existing project**.
2. Pick GitHub and this repository.
3. In the settings that appear:
   * **Base directory:** `tv`
   * Leave **Build command** and **Publish directory** as they are (they come
     from `tv/netlify.toml`).
4. Open **Add environment variables** (or later: **Site configuration >
   Environment variables**) and add one:
   * **Key:** `APP_URL`
   * **Value:** your store app's web address, for example
     `https://your-app.onrender.com`
5. Click **Deploy**.

If the deploy stops with "STOPPED: APP_URL is not set", go back to step 4, add
it, then **Deploys > Trigger deploy**.

Netlify rebuilds the TV page when something in this folder changes, or when
`APP_URL` has changed. Changes elsewhere in the repository (the store app) don't
rebuild it. **If the store app moves to a new address:** change `APP_URL` in
Netlify (step 4), then **Deploys > Trigger deploy**. The TVs pick up the new
address at their next hourly reload.

**Check it:** open your Netlify address (it looks like
`https://something.netlify.app`). You should see the numbers within a few
seconds. To preview with sample numbers instead, add `?demo=1` to the end of
the address: it says **DEMO DATA** in the corner. Never put the `?demo=1`
address on a TV.

## 2. Put it on the TVs (AbleSign)

1. In AbleSign, add a new **Web page / URL** item.
2. Paste your Netlify address, exactly as it is (no `?demo=1`).
3. Set it to full screen, landscape, and to stay on (no end time).
4. Add it to each TV's playlist or schedule.

The page fits any screen size (1920x1080, 1280x720 or 4K). Nothing needs to be
clicked.

## What the screen does on its own

* **Checks for new numbers every 2 minutes.** New results appear without
  anyone touching anything.
* **Reloads itself every hour**, so it always ends up current even if the TV
  has been running for days. While reloading it keeps showing the last
  numbers.
* **Keeps the last numbers through internet outages.** If the internet or the
  store app is down, the TV keeps the numbers it already has and keeps
  trying. The TV also keeps a copy of the page itself, so after a power cut it
  opens and shows the last saved numbers straight away, even if the internet
  isn't back yet. (That works once the TV has shown the page with the internet
  working at least once. A brand new TV needs the internet the first time.) If
  a TV ever shows a "no internet" page instead, restart it once the internet
  is back.
* **Never makes up numbers.** If it has never received any (a brand new TV
  with no internet yet), it shows "Today's numbers are on their way" until
  real ones arrive.
* Shows **TONIGHT!** on a game's draw day (Pennsylvania time).
* A small "Updated 8:20 PM" note in the bottom-left corner says when the
  numbers were last refreshed.
* If PA adds a new game, it appears by itself with its name (no logo or price
  until one is added to `games.json`).

## Changing prices, colours or logos

Everything about how each game looks is in `games.json`, one entry per PA game
number:

* `price`: the yellow ticket-price callout (leave it out to show no price).
  Prices today come from PA's Game Guide (April 2026).
* `color`: the game's colour block.
* `logo` and `logoSize`: the logo file in `logos/` and its size in pixels.

Save the change in GitHub; Netlify publishes it and the TVs pick it up at their
next hourly reload. TVs keep logo and font files for up to a day, so to swap a
logo straight away, give the new file a new name and change `logo` to match.

## For testing

* `?demo=1` uses the sample numbers in `sample-numbers.json` (real PA results
  from October 28, 2026). Only for previews.
* `?api=https://your-app.onrender.com/api/numbers` reads the numbers straight
  from an app address instead of through Netlify.

## Fonts

Montserrat and Anton, self-hosted in `fonts/`, under the SIL Open Font License
(see `fonts/OFL.txt`).
