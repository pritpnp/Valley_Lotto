"""Sample-game helpers for tests.

A real game always arrives with PA's full printed prize table, the tickets
printed and PA's published odds, and the code only trusts the table when those
agree. Hand-made samples need the same, so the maths under test gets used.
"""


def verified(g, odds: float | None = None):
    """Give a sample game a ticket count and odds that agree with its table."""
    o = odds or g.odds_value or 3.5
    g.odds = f"1:{o:g}"
    g.tickets_printed = round(sum(g.tier_originals.values()) * o)
    return g


def verified_dict(d: dict, odds: float | None = None) -> dict:
    """The same, for a game written as a dict (a state.json entry)."""
    o = odds or float(str(d.get("odds", "1:3.5")).split(":")[1])
    d["odds"] = f"1:{o:g}"
    d["tickets_printed"] = round(sum(d["tier_originals"].values()) * o)
    return d
