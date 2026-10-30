"""Put ``src/`` on the import path for every test run.

Without this the suite only worked because the alphabetically-first module
happened to insert the path itself, so running a single test file failed.

Also: the app reads PA's numbers in the background when /api/numbers is asked
for (pa_feeds/in_app.py). That's off in the tests, which have no internet and
would leave runs going after they end; a test that wants it turns it on
(create_app({..., "PA_FEED_IN_APP": True}) or PA_FEED_IN_APP=1).
"""

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ["PA_FEED_IN_APP"] = "0"
