# OrderBOT developer handoff

OrderBOT is a Python 3.11+ paper trading system for NSE cash stocks and NIFTY options. The cash scanner covers stocks with listed derivatives. The options strategies take signals from finalized one-minute NIFTY near-month futures candles and evaluate fills against the selected option book. Both account budgets are unset by default.

[Setup and operating instructions](INSTRUCTIONS.md) · [Strategies and measured results](STRATEGIES.md)

## Functionality

| Function | Entry point and behavior |
| --- | --- |
| Dashboard | `python run.py --dashboard` serves the local Stocks, NIFTY Options, Backtest, Settings and Background Jobs screens. The dashboard displays exchange timestamp freshness separately from the time of the HTTP fetch. |
| Stock scanner | Reads the dated instrument master, joins NSE cash tokens to listed stock futures, and ranks cash movers. It exposes gainers, losers, breadth and a watchlist. The options chain uses NIFTY spot to center calls and puts. |
| Session rules | The dated rules in `market.nse_schedules` separate stock continuous trading, the cash closing auction, the cash post-close session, and F&O trading. Cash strategies accept fills only during continuous trading. |
| Stock strategy and journal | Evaluates stocks in play, opening-range breakouts and optional VWAP entries. The journal records simulated entries, partial exits, fees and net P&L. Unfilled exit shares survive restarts. |
| Options strategy and execution | A futures candle can produce a signal. Option selection checks contract metadata, liquidity, spread and modeled Greeks. Risk and position sizing can veto the signal. Paper execution handles timeout lookup, reconciliation, exits and kill switches. |
| Historical data | Stock baselines, charts and backtests use the imported candle store and the workspace’s SmartAPI history cache. On-demand reads can use SmartAPI for uncovered windows when connected. The background baseline job uses local data by default. |
| Replays and research | Fixture replays exercise the options pipeline offline. Research strategies and dashboard stock backtests have separate entry points and fill assumptions. |
| Live-data verification | The read-only probe samples actual quotes and finalized minute history through the running paper dashboard’s broker session. It records quote age, missing candles, and any overlapping local candle differences. |
| Reports and alerts | SQLite is the trading journal. Reporting reconciles paper orders, P&L, fees and trade outcomes. Telegram delivery is disabled by default. |

## Repository map

| Path | Responsibility |
| --- | --- |
| `run.py`, `config.yaml` | CLI, paper-only startup, typed application and session settings |
| `src/orderbot/core/` | Configuration, IST timestamps, SQLite migrations, activity and structured logging |
| `src/orderbot/broker/` | SmartAPI login, request pacing, cooldowns, WebSocket decoding, instrument master and history cache |
| `src/orderbot/data/` | Quote scanner, session windows, local candle store, indicators, tick archive, capture and verification |
| `src/orderbot/strategy/` | Stock strategy, options strategies, historical replays and real-data paper trial |
| `src/orderbot/options/` | Expiry resolution, strike selection, depth, Greeks, and freeze quantities |
| `src/orderbot/risk/`, `src/orderbot/execution/` | Costs, sizing, risk checks, paper execution, exits and reconciliation |
| `src/orderbot/reporting/`, `src/orderbot/reporting/web/` | Loopback dashboard API, HTML/CSS/JS, reports and alerts |
| `research/intraday_strategies/` | Standalone strategy experiments and candle-based simulator |
| `tests/`, `tests/fixtures/` | Regression tests and synthetic scenarios |

Daily workflow uses the instrument master for tokens, lot sizes and listed expiries. The feed or history response is validated before generating a signal. Risk and execution then update the journal. The dashboard and reports read that journal. Real-time quote state is a separate input and ages even when the dashboard is polling successfully.
