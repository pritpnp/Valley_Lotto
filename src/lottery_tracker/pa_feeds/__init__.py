"""What Pennsylvania publishes for its retailers, read on a schedule.

* ``draws``: the winning numbers of every draw game (PA's feed, plus the old
  Powerball/Mega Millions scraper while the two are checked side by side).
* ``orders``: the weekly "Scratch-Offs Available for Ordering" PDF.
* ``store``: saving both in the app's database, and telling Telegram, once.
* ``display``: the feed the store TVs show (the app's ``/api/numbers``).

Run with ``python -m lottery_tracker.pa_feeds numbers|orders|all``. The full
description is in ``docs/pa-feeds-spec.md``.
"""
