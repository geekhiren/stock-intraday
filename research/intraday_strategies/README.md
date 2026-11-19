# Published intraday strategies, one file each

Seven strategies from the Flattrade Kosh article [Common Intraday Trading Strategies Used in Indian Markets](https://flattrade.in/kosh/common-intraday-trading-strategies-used-in-indian-markets/), each in its own file so it can be read and tested alone. Every file starts with `RULES`: what the article says, then what had to be assumed because the article gives no number.

| File | Strategy |
| --- | --- |
| `s1_momentum.py` | Early-morning break of the session high or low with volume and RSI |
| `s2_breakout.py` | Break out of a tight two-hour range on volume |
| `s3_reversal.py` | Fade an RSI extreme back to VWAP |
| `s4_vwap_pullback.py` | Buy or short a pullback to VWAP in the day's trend |
| `s5_scalping.py` | One-minute scalps with a Rs 2 stop and Rs 3 target |
| `s6_gap_and_go.py` | Trade an opening gap that holds for 15 minutes |
| `s7_opening_range_breakout.py` | Break of the first 15 minutes' range |

```powershell
python -m research.intraday_strategies.run --symbol RELIANCE
python -m research.intraday_strategies.run --symbol HDFCBANK --only s7_opening_range_breakout --trades
python -m pytest tests/test_intraday_strategies.py
```

The tester in `common.py` enters at the signal candle's close plus one tick, checks later one-minute candles for the stop and target (the stop wins when a candle touches both), adds one tick of slippage on stops, closes everything by 15:10 and charges full Indian intraday costs. Sizing is the bot's: the rupee risk and position size from `config.yaml`, one position at a time.

## RELIANCE, 21/08/2026 to 18/11/2026 (61 sessions)

Rs 500 risk per trade, at most Rs 50,000 per position.

| Strategy | Trades | Accuracy | Net P&L | Before charges | Average win | Average loss |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Momentum | 6 | 16.7% | −Rs 666.90 | −Rs 278.60 | Rs 94.48 | −Rs 152.28 |
| Range breakout | 26 | 42.3% | −Rs 134.02 | Rs 1,548.70 | Rs 202.39 | −Rs 157.35 |
| Reversal to VWAP | 7 | 0.0% | −Rs 1,107.01 | −Rs 653.89 | Rs 0.00 | −Rs 158.14 |
| VWAP pullback | 50 | 34.0% | −Rs 2,499.96 | Rs 736.89 | Rs 274.36 | −Rs 217.09 |
| Scalping (candle approximation) | 437 | 13.7% | −Rs 29,727.12 | −Rs 1,431.70 | Rs 38.12 | −Rs 84.92 |
| Gap and go | 1 | 100.0% | Rs 406.43 | Rs 461.70 | Rs 406.43 | Rs 0.00 |
| Opening range breakout | 57 | 38.6% | −Rs 3,802.16 | −Rs 208.45 | Rs 265.88 | −Rs 275.76 |

One stock over three months is a demonstration, not proof. Gap and go has a single trade. Scalping as the article describes it needs the live order book, so its row tests only the article's stop and target sizes on candles. The article states no backtest results of its own.
