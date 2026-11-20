# Strategies and measured results

## Dashboard and options engine

| Strategy | Signal and exit behavior | Test evidence |
| --- | --- | --- |
| Options Strategy A, VWAP and RSI | Uses finalized NIFTY near-month futures candles. Checks candle closure, entry window, VWAP crossover, consolidation, volume, RSI, pivot proximity, option liquidity and IV. An eligible call or put uses the configured stop plan and paper exit rules. | Deterministic fixture replays. Market-hours profitability and win rate remain unmeasured. |
| Options Strategy B, opening range | Freezes the opening range from futures candles. Direction requires the opening edge and prior-close gap to agree. The first eligible break enters through option liquidity and risk checks. In `ohl` mode, a fill blocks Strategy A for the remainder of the day. | Fixture tests cover entry, no-entry and restart precedence. Market-hours profitability and win rate remain unmeasured. |
| Stocks in play | Ranks eligible stocks by relative opening volume against their historical baseline. A qualifying break follows the direction of the opening move, with an ATR-based stop and square-off. | Entry, sizing, stop and square-off fixtures. No audited out-of-sample accuracy figure in this handoff. |
| Stock opening range and optional VWAP crossover | Opening-range breakout trades require the configured gap and edge checks. The optional VWAP crossover uses volume, trend and RSI. Dashboard backtests replay these rules with modeled fills. | Fixtures cover long and short examples, depth, stops, fees and session limits. Accuracy depends on the symbol, date range and budget. |

Sizing, costs, caps and quote freshness can reject a valid strategy signal. The code records those decisions separately. The broker client only logs in and reads market data.

ATR and watchlist baselines skip incomplete cash sessions. The stock replay can use the complete prefix before the first missing minute. It skips later entries for that stock because its cumulative volume and VWAP are no longer reliable. A position that crosses a gap is excluded from P&L, and the replay pauses new portfolio entries for that session because its realized P&L is unknown. Stops and targets in complete windows still use the observed candles. Live VWAP warm-up resets when a stock is missing from a scan.

## Standalone research backtests

The table is a local-data replay on RELIANCE and HDFCBANK from 21 August through 18 November 2026. Each series contained 60 regular sessions after applying the cash-session filter. The replay used synthetic ₹500 risk per trade and ₹50,000 per position, one concurrent position per stock, and modeled charges. Accuracy is the percentage of completed trades with positive net P&L.

| Symbol | Research strategy | Closed trades | Win rate | Net P&L |
| --- | --- | ---: | ---: | ---: |
| RELIANCE | Range breakout | 26 | 42.3% | −₹134.02 |
| RELIANCE | Gap and go | 1 | 100.0% | ₹406.43 |
| HDFCBANK | Range breakout | 26 | 26.9% | −₹1,127.86 |
| HDFCBANK | Gap and go | 1 | 100.0% | ₹247.72 |

Range breakout tests a break of a tight two-hour range on elevated volume. Gap and go uses a complete 15-minute opening range, a gap from the previous close and opening volume compared with ten prior sessions. Both strategies run separately from the dashboard executor. The two one-trade samples provide no reliable win-rate estimate. Range breakout lost money after charges on both symbols in this replay.

The research simulator uses candle closes, modeled slippage and a stop-first assumption when a candle touches both stop and target. It drops incomplete aggregate bars and excludes trades whose exit depends on a missing candle. Gap and go requires contiguous bars while waiting for the first breakout. Real quote depth and queue position are outside that model. Earlier experiments and their removal decisions are recorded in the [research README](../research/intraday_strategies/README.md).
