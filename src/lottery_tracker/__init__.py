"""Valley Lotto — PA Lottery scratch-off tracker.

Watches the PA Lottery "Sales Ended" and "Prizes Remaining" print pages and
tells a retailer when:

  * a game has ended sales (especially one they still carry), and
  * a game's prizes have run too low to be worth keeping on the counter.

The package is intentionally small and dependency-light (requests + bs4 + PyYAML).
"""

__version__ = "1.0.0"
