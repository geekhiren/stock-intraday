"""Strategy 1: Momentum trading."""
from .common import Day, Plan, average

NAME = "Momentum"
RULES = """
Article (Flattrade Kosh): on 5- or 15-minute candles, find stocks moving strongly early (09:15-10:00). Buy when
price breaks above a recent high, short when it breaks below a recent low, with high volume and RSI above 60 for
bullish momentum. Stop 0.5% to 1% from entry. Exit when momentum slows (volume drops, price stalls).

Assumed where the article gives no number: 5-minute candles; "recent high/low" is the session's high/low so far;
high volume is 1.5 times the session's average 5-minute volume; RSI below 40 for shorts; stop 0.75%; "momentum
slows" is the first 5-minute candle that closes beyond the previous candle's low (for a buy) or high (for a short);
one trade a day.
"""
STOP_PCT, VOLUME_MULTIPLE = 0.75, 1.5


def plans(day: Day) -> list[Plan]:
    bars = day.bars(5)
    for index in range(2, len(bars)):
        bar, before = bars[index], bars[:index]
        if bar.clock >= "10:00":
            break
        if bar.rsi is None or bar.volume < VOLUME_MULTIPLE * average(item.volume for item in before):
            continue
        side = 1 if bar.close > max(item.high for item in before) and bar.rsi > 60 else \
            -1 if bar.close < min(item.low for item in before) and bar.rsi < 40 else 0
        if not side:
            continue
        stall = next((later.end for earlier, later in zip(bars[index:], bars[index + 1:])
                      if side * (later.close - (earlier.low if side > 0 else earlier.high)) < 0), None)
        return [Plan(bar.end, side, bar.close * (1 - side * STOP_PCT / 100), exit_at=stall)]
    return []
