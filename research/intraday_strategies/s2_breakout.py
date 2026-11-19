"""Strategy 2: Breakout from a tight range."""
from .common import Day, Plan, _offset, average

NAME = "Range breakout"
RULES = """
Article (Flattrade Kosh): on 15-minute candles, find a stock stuck in a tight range for the past few hours
(example: 500 to 510, about 2%). Buy when price breaks above the range on above-average volume, short when it
breaks below. Stop just below the breakout point. Target the next resistance level.

Assumed: "a few hours" is the last 8 fifteen-minute candles (2 hours); tight means the range is at most 1% of
price; above-average volume is 1.5 times the range's average; the stop is the middle of the range; with no
resistance level to read from a chart, the target is twice the risk; one trade a day.
"""
LOOKBACK, MAX_RANGE_PCT, VOLUME_MULTIPLE, REWARD = 8, 1.0, 1.5, 2.0


def plans(day: Day) -> list[Plan]:
    bars = day.bars(15)
    for index in range(LOOKBACK, len(bars)):
        window, bar = bars[index - LOOKBACK:index], bars[index]
        if _offset(bar.clock) - _offset(window[0].clock) != 15 * LOOKBACK:
            continue
        high, low = max(item.high for item in window), min(item.low for item in window)
        if (high - low) / low * 100 > MAX_RANGE_PCT or bar.volume < VOLUME_MULTIPLE * average(item.volume for item in window):
            continue
        side = 1 if bar.close > high else -1 if bar.close < low else 0
        if side:
            stop = (high + low) / 2
            return [Plan(bar.end, side, stop, target=bar.close + REWARD * (bar.close - stop))]
    return []
