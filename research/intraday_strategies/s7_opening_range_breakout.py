"""Strategy 7: Opening range breakout (ORB)."""
from .common import Day, Plan

NAME = "Opening range breakout"
RULES = """
Article (Flattrade Kosh): mark the high and low of the first 15 minutes (09:15-09:30). Buy if price breaks above
the high, short if it breaks below the low. The stop is the opposite end of the range. The target is 1.5 to 2
times the range size.

Assumed: a break is a 5-minute close beyond the range; the target is 1.5 times the range from entry; one trade a
day, the first break.
"""
TARGET_RANGES = 1.5


def plans(day: Day) -> list[Plan]:
    opening = [bar for bar in day.minutes if bar.clock < "09:30"]
    if len(opening) < 10:
        return []
    high, low = max(bar.high for bar in opening), min(bar.low for bar in opening)
    for bar in day.bars(5):
        if bar.clock < "09:30":
            continue
        side = 1 if bar.close > high else -1 if bar.close < low else 0
        if side:
            return [Plan(bar.end, side, low if side > 0 else high, target=bar.close + side * TARGET_RANGES * (high - low))]
    return []
