"""Put ``src/`` on the import path for every test run.

Without this the suite only worked because the alphabetically-first module
happened to insert the path itself, so running a single test file failed.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
