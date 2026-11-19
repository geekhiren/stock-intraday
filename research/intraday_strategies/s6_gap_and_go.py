"""Strategy 6: Gap and go."""
from .common import Day, Plan, average

NAME = "Gap and go"
RULES = """
Article (Flattrade Kosh): find stocks that gap up or down at the open on strong volume. Wait the first 5-15 minutes
to see if the gap holds. If a gap up keeps going, buy; if a gap down keeps going, short. Exit quickly if a gap up
starts falling. The article gives no gap size, stop or target numbers.

Assumed: a gap is an open at least 1% from the previous close; strong volume is first-15-minute volume above 1.5
times its average over the previous 10 sessions; the gap holds when the first 15 minutes stay beyond the previous
close and end beyond the open; entry is a 5-minute close beyond the first 15 minutes' extreme; the stop is the other
end of those 15 minutes; with no target, the trade runs to the stop or the square-off; one trade a day.
"""
MIN_GAP_PCT, VOLUME_MULTIPLE, HISTORY = 1.0, 1.5, 10


def _opening(day: Day):
    return [bar for bar in day.minutes if bar.clock < "09:30"]


def plans(day: Day) -> list[Plan]:
    opening, earlier = _opening(day), day.earlier[-HISTORY:]
    if day.previous_close is None or len(opening) < 10 or len(earlier) < HISTORY:
        return []
    gap = (opening[0].open / day.previous_close - 1) * 100
    side = 1 if gap >= MIN_GAP_PCT else -1 if gap <= -MIN_GAP_PCT else 0
    usual = average(sum(bar.volume for bar in _opening(item)) for item in earlier)
    if not side or sum(bar.volume for bar in opening) < VOLUME_MULTIPLE * usual:
        return []
    high, low = max(bar.high for bar in opening), min(bar.low for bar in opening)
    held = (low > day.previous_close and opening[-1].close >= opening[0].open) if side > 0 else \
        (high < day.previous_close and opening[-1].close <= opening[0].open)
    if not held:
        return []
    for bar in day.bars(5):
        if bar.clock >= "09:30" and side * (bar.close - (high if side > 0 else low)) > 0:
            return [Plan(bar.end, side, low if side > 0 else high)]
    return []
