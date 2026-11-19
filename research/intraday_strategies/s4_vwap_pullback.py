"""Strategy 4: VWAP trading (pullback and bounce)."""
from .common import Day, Plan

NAME = "VWAP pullback"
RULES = """
Article (Flattrade Kosh): on 5-minute candles, price above VWAP means the stock is bullish for the day, so look to
buy; below VWAP means bearish, so look to short. A pullback to VWAP that bounces is a good entry. The article gives
no stop, target or time rules.

Assumed: the trend is set when the last 6 five-minute closes are all on one side of VWAP; a pullback is a candle
that comes within 0.1% of VWAP and closes back on the trend side in the trend's colour; the stop is 0.3% beyond
VWAP; the target is twice the risk; entries from 09:45; up to two trades a day.
"""
TREND_BARS, TOUCH_PCT, STOP_PCT, REWARD, MAX_TRADES = 6, 0.1, 0.3, 2.0, 2


def plans(day: Day) -> list[Plan]:
    bars, found = day.bars(5), []
    for index in range(TREND_BARS, len(bars)):
        bar, vwap = bars[index], day.vwap[bars[index].end]
        if bar.clock < "09:45" or len(found) >= MAX_TRADES:
            continue
        sides = {1 if item.close > day.vwap[item.end] else -1 for item in bars[index - TREND_BARS:index]}
        if len(sides) != 1:
            continue
        side = sides.pop()
        touched = (bar.low <= vwap * (1 + TOUCH_PCT / 100)) if side > 0 else (bar.high >= vwap * (1 - TOUCH_PCT / 100))
        bounced = side * (bar.close - vwap) > 0 and side * (bar.close - bar.open) > 0
        if touched and bounced:
            stop = vwap * (1 - side * STOP_PCT / 100)
            found.append(Plan(bar.end, side, stop, target=bar.close + REWARD * (bar.close - stop)))
    return found
