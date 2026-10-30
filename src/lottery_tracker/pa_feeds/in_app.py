"""PA's winning numbers, read by the app itself whenever the TVs ask for them.

GitHub's scheduler is best-effort: on a busy day it ran the "every 10 minutes"
numbers job 6 times in 18 hours, and the store TVs showed numbers hours old
after a draw. So the app reads PA too. When a TV asks for ``/api/numbers``
and no numbers run, the app's or GitHub's, has started or finished in the last
5 minutes, one starts in the background. The TV is answered at once with what's saved and never waits for
PA; the new numbers are there for its next check.

* **One run at a time.** In this app, a lock in memory; across the app and the
  GitHub job (and two copies of the app, while Render swaps in a new one), the
  lock in the database (``store.numbers_lock``).
* **Quiet.** Render has no Telegram settings, so a run here never sends a
  message, and never drops one either: each waits in the database, saved with
  what it's about, and the next GitHub run delivers it, once (``store.quiet``).
  The daily "the database has a problem" message is the GitHub job's alone.
* **Never takes the app down.** Anything that goes wrong is caught and logged
  (never the database link), and the run's database session and connections
  are always closed. A run that fails is tried again 5 minutes after it
  started, never at the next TV check, even one that failed before it could
  save that it had started.
* ``PA_FEED_IN_APP=0`` on Render turns it off (then only the GitHub job reads
  PA). The tests have it off unless a test turns it on.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from sqlalchemy.exc import DBAPIError

from . import __main__ as job
from . import store

log = logging.getLogger(__name__)

BUSY = ("PA's numbers: another run (most likely the GitHub job) is reading them right "
        "now, so the app leaves them to it.")

_OFF = {"0", "false", "no", "off"}


def wanted(value) -> bool:
    """``PA_FEED_IN_APP``: on unless it says 0, false, no or off."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().strip("\"'").strip().lower() not in _OFF


def run_once(session_factory, now: datetime | None = None) -> str:
    """One quiet numbers run, on this thread, with its own database session.
    Returns what happened: ``"done"``, ``"busy"`` (another run has the lock) or
    ``"fresh"`` (another run read PA in the last 5 minutes, just before this
    one got the lock)."""
    now = now or store.utcnow()
    with session_factory() as session:
        with store.numbers_lock(session.get_bind()) as mine:
            if not mine:
                return "busy"
            if not store.numbers_due(session, now):
                return "fresh"
            store.quiet(session)
            store.numbers_started(session, now)
            job.numbers(session, now)
            store.numbers_finished(session, now)
            session.commit()
    return "done"


class Refresher:
    """The app's one background reader of PA's numbers (one per app)."""

    def __init__(self, session_factory, logger: logging.Logger | None = None):
        self.session_factory = session_factory
        self.log = logger or log
        self._running = threading.Lock()    # held from a run's start to its end
        self.thread: threading.Thread | None = None    # the last run started
        # When the last run that failed started. A run usually saves its start
        # first thing, and that's what spaces runs 5 minutes apart. One that
        # failed before it could (a database that has gone read-only, or won't
        # hand out a connection) saved nothing, so without this every TV check
        # would start another, each failing and logging the same warning.
        self._failed_at: datetime | None = None

    def maybe_start(self, session, now: datetime | None = None) -> bool:
        """Start a run in the background if one is due, and return at once.
        ``session`` is the request's own, for the one small query. Never raises:
        whatever goes wrong, the TV still gets its numbers. Returns True if a
        run was started."""
        if not self._running.acquire(blocking=False):
            return False                     # this app is already reading PA
        started = False
        try:
            now = now or store.utcnow()
            failed_at = self._failed_at
            if failed_at is not None and abs(now - failed_at) < store.NUMBERS_FRESH:
                return False                 # the last one failed: wait as if it had run
            if store.numbers_due(session, now):
                thread = threading.Thread(target=self._run, args=(now,),
                                          name="pa-numbers", daemon=True)
                thread.start()
                self.thread, started = thread, True
        except Exception as e:  # noqa: BLE001 — the TV's answer comes first
            self.log.warning("Couldn't check whether PA's numbers are due (%s).",
                             type(e).__name__)
        finally:
            if not started:
                self._running.release()
        return started

    def _run(self, now: datetime) -> None:
        try:
            result = run_once(self.session_factory, now)
            self._failed_at = None
            if result == "busy":
                print(BUSY, flush=True)
        except Exception as e:  # noqa: BLE001 — a background run must never stop the app
            self._failed_at = now
            # A database error by its kind only: its message names the server
            # and the user. Anything else is a bug, so the whole story is kept.
            self.log.warning("Reading PA's numbers in the background failed (%s). "
                             "It's tried again 5 minutes after it started.",
                             type(e).__name__, exc_info=not isinstance(e, DBAPIError))
        finally:
            self._running.release()
