# Published intraday strategies, one file each

Strategies from the Flattrade Kosh article [Common Intraday Trading Strategies Used in Indian Markets](https://flattrade.in/kosh/common-intraday-trading-strategies-used-in-indian-markets/), each in its own file so it can be read and tested alone. Every file starts with `RULES`: what the article says, then what had to be assumed because the article gives no number.

The article lists seven. Five were removed on 19/11/2026 because their accuracy on the RELIANCE test below was under 40%. Their code is in git history (commit `df2c279`).

| File | Strategy |
| --- | --- |
| `s2_breakout.py` | Break out of a tight two-hour range on volume |
| `s6_gap_and_go.py` | Trade an opening gap that holds for 15 minutes |

```powershell
python -m research.intraday_strategies.run --symbol RELIANCE
python -m research.intraday_strategies.run --symbol HDFCBANK --only s2_breakout --trades
python -m pytest tests/test_intraday_strategies.py
```

The tester in `common.py` enters at the signal candle's close plus one tick, checks later one-minute candles for the stop and target (the stop wins when a candle touches both), adds one tick of slippage on stops, closes everything by 15:10 and charges full Indian intraday costs. Sizing is the bot's: the rupee risk and position size from `config.yaml`, one position at a time.

Set the stock paper capital and risk per trade to run a sized backtest. Five- and fifteen-minute candles require every constituent minute. Gap-and-go also requires complete opening ranges for the current day and its volume baseline.

## RELIANCE, 21/08/2026 to 18/11/2026 (61 sessions)

Rs 500 risk per trade, at most Rs 50,000 per position.

| Strategy | Trades | Accuracy | Net P&L | Before charges | Kept |
| --- | ---: | ---: | ---: | ---: | --- |
| Momentum | 6 | 16.7% | −Rs 666.90 | −Rs 278.60 | Removed |
| Range breakout | 26 | 42.3% | −Rs 134.02 | Rs 1,548.70 | Yes |
| Reversal to VWAP | 7 | 0.0% | −Rs 1,107.01 | −Rs 653.89 | Removed |
| VWAP pullback | 50 | 34.0% | −Rs 2,499.96 | Rs 736.89 | Removed |
| Scalping (candle approximation) | 437 | 13.7% | −Rs 29,727.12 | −Rs 1,431.70 | Removed |
| Gap and go | 1 | 100.0% | Rs 406.43 | Rs 461.70 | Yes |
| Opening range breakout | 57 | 38.6% | −Rs 3,802.16 | −Rs 208.45 | Removed |

One stock over three months is a demonstration, not proof. Accuracy differs by stock: on HDFCBANK over the same sessions, opening range breakout scored 50.9%, VWAP pullback 46.3% and reversal 57.1%, while range breakout scored 26.9%. Gap and go has a single trade on each stock. Neither kept strategy has shown a profit that can be relied on: range breakout lost money after charges on both stocks.
