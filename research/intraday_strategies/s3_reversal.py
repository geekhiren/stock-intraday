"""Strategy 3: Reversal (mean reversion)."""
from .common import Day, Plan

NAME = "Reversal to VWAP"
RULES = """
Article (Flattrade Kosh): after a very sharp move, look for exhaustion: RSI above 75-80 (overbought) or below
20-25 (oversold), long wicks, a doji, shooting star or engulfing candle, falling volume. Enter against the move
with a tight stop. A common target is price coming back to VWAP.

Assumed: 5-minute candles; RSI above 80 with a red candle while above VWAP gives a short, RSI below 20 with a green
candle while below VWAP gives a buy; the stop is just beyond that candle's extreme; the target is VWAP at entry;
the trade is skipped when VWAP is closer than the stop (reward below risk); up to two trades a day.
"""
OVERBOUGHT, OVERSOLD, MAX_TRADES = 80, 20, 2


def plans(day: Day) -> list[Plan]:
    found = []
    for bar in day.bars(5):
        vwap = day.vwap[bar.end]
        if bar.rsi is None or bar.clock < "09:30" or len(found) >= MAX_TRADES:
            continue
        side = -1 if bar.rsi > OVERBOUGHT and bar.close < bar.open and bar.close > vwap else \
            1 if bar.rsi < OVERSOLD and bar.close > bar.open and bar.close < vwap else 0
        if not side:
            continue
        stop = bar.high * 1.0005 if side < 0 else bar.low * 0.9995
        if abs(vwap - bar.close) >= abs(stop - bar.close):
            found.append(Plan(bar.end, side, stop, target=vwap))
    return found
