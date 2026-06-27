"""Valley Lotto multi-store web app.

A small, self-hostable web app that sits on top of the scratch-off tracker.
Every store gets its own login, its own staff, and its own inventory list, while
they all share ONE Pennsylvania scratch-off database (the data the scraper in
``lottery_tracker`` produces twice a day). Each store sees a dashboard scoped to
the games it actually carries, with the same KEEP / SEND-BACK calls and metrics
as the static report — plus a catalog-wide "bring in" board.

Design goals: no external services required, runs as a single process against a
SQLite file, passwords hashed with stdlib scrypt. Portable enough to run on a
Beelink mini-PC behind a Cloudflare Tunnel or any container host.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
