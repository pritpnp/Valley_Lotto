"""What a person is allowed to do, one capability at a time.

Roles alone were too blunt. Some people need part of a manager's job — the one
who receives deliveries, or the shift lead who settles a pack — without getting
staff administration or pricing along with it. So a manager keeps everything by
virtue of being a manager, and an employee gets exactly the boxes ticked next to
their name.

Deliberately a *flat list of grants*, not a hierarchy: "can receive shipments"
should never quietly imply "can delete staff". A capability nobody ticks is
simply not held.
"""

from __future__ import annotations

# key, label, and what granting it actually lets someone do. The order here is
# the order of the checkboxes on the staff page, grouped roughly by how often a
# store hands each one out.
CAPABILITIES = (
    {"key": "count", "label": "Run counts",
     "blurb": "Scan the boxes. Everyone has this — it's the job.",
     "always": True},
    {"key": "reports", "label": "See daily sales",
     "blurb": "The daily report: tickets sold and revenue, per box and per game."},
    {"key": "backstock", "label": "Count unopened packs",
     "blurb": "Scan the packs not yet in a box and see what the store holds."},
    {"key": "receive", "label": "Receive shipments",
     "blurb": "Log a delivery and scan the packs in it."},
    {"key": "settle", "label": "Settle & return packs",
     "blurb": "Take a pack out of play and record it as settled and returned."},
    {"key": "counts_edit", "label": "Correct past counts",
     "blurb": "Fix a ticket number after the fact, or type in a count done on "
              "paper. Every change is recorded."},
    {"key": "boxes", "label": "Edit active inventory",
     "blurb": "Change which game is assigned to a box by hand."},
    {"key": "history", "label": "See history",
     "blurb": "Past days, per-game trends, and the change log."},
    {"key": "pricing", "label": "Tune the ratings",
     "blurb": "The emphasis sliders that decide keep vs send-back."},
    {"key": "staff", "label": "Manage staff PINs",
     "blurb": "Add people, reset PINs, and set these permissions. Hand out sparingly."},
    {"key": "access", "label": "See the access log",
     "blurb": "Every device that reached the site, and every PIN attempt."},
)

#: Capabilities that can be granted individually (i.e. everything but "count",
#: which everyone holds).
GRANTABLE = tuple(c for c in CAPABILITIES if not c.get("always"))

ALL_KEYS = tuple(c["key"] for c in CAPABILITIES)
_ALWAYS = {c["key"] for c in CAPABILITIES if c.get("always")}
_VALID = set(ALL_KEYS)


def label_for(key: str) -> str:
    for c in CAPABILITIES:
        if c["key"] == key:
            return c["label"]
    return key


def parse(stored: str | None) -> set:
    """Read the stored permission string into a set of keys.

    Unknown keys are dropped rather than trusted — a capability removed from the
    code must not keep granting anything through old rows.
    """
    keys = {p.strip() for p in (stored or "").split(",") if p.strip()}
    return (keys & _VALID) | set(_ALWAYS)


def dump(keys) -> str:
    """Store a set of keys, in the canonical order, without the implicit ones."""
    chosen = set(keys) & _VALID
    return ",".join(k for k in ALL_KEYS if k in chosen and k not in _ALWAYS)


def describe(stored: str | None) -> str:
    """A short human summary for the staff list: 'Counts + daily sales'."""
    granted = [k for k in ALL_KEYS if k in parse(stored) and k not in _ALWAYS]
    if not granted:
        return "Counts only"
    return "Counts + " + ", ".join(label_for(k).lower() for k in granted)
