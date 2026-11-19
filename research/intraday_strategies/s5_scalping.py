"""Strategy 5: Scalping."""
from .common import Day, Plan, average

NAME = "Scalping (candle approximation)"
RULES = """
Article (Flattrade Kosh): on 1-minute charts of very liquid stocks, read the order book and the tape, enter and
exit within minutes or seconds, use very tight stops (often Rs 1-2) and aim for Rs 1-5 of profit per share with a
high win rate.

This cannot be tested as written: it relies on the live order book and tape, which candles do not contain.
Assumed, to test the stated stop and target sizes: buy when a 1-minute candle closes above the previous three
candles' highs on twice the average volume of the last 20 candles while above VWAP (mirror for shorts); stop Rs 2,
target Rs 3, out after 5 minutes; from 09:30; at most 10 trades a day.
"""
STOP_RUPEES, TARGET_RUPEES, HOLD_MINUTES, MAX_TRADES = 2.0, 3.0, 5, 10


def plans(day: Day) -> list[Plan]:
    bars, found = day.minutes, []
    for index in range(20, len(bars) - HOLD_MINUTES):
        bar, recent = bars[index], bars[index - 3:index]
        if bar.clock < "09:30" or len(found) >= MAX_TRADES or bar.volume < 2 * average(item.volume for item in bars[index - 20:index]):
            continue
        vwap = day.vwap[index]
        side = 1 if bar.close > max(item.high for item in recent) and bar.close > vwap else \
            -1 if bar.close < min(item.low for item in recent) and bar.close < vwap else 0
        if side:
            found.append(Plan(index, side, bar.close - side * STOP_RUPEES, target=bar.close + side * TARGET_RUPEES,
                              time_exit=bars[index + HOLD_MINUTES].clock))
    return found
