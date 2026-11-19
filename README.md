# OrderBOT

OrderBOT uses Python 3.11+ and runs in paper mode. The command-line entry point initializes storage, checks SmartAPI authentication, and captures or replays market data for the NIFTY near-month futures signal series.

```powershell
python -m pip install -e ".[test]"
python run.py --check-config
python run.py
python -m pytest
```

`config.yaml` leaves capital and per-trade risk unset. Relative data and log paths resolve beside the YAML file. SQLite timestamps use explicit UTC offsets. The JSON logs use IST.

## Starting the paper dashboard each morning

`start-orderbot.cmd` starts `python run.py --dashboard` from the project folder and appends its console output to `var/log/dashboard-console.log`. A weekday Windows scheduled task can run it before the open:

```powershell
schtasks /Create /TN "OrderBOT paper dashboard" /TR "<full path to start-orderbot.cmd>" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:00
```

The task runs only while you are signed in to Windows. A second start exits at once because the dashboard port is in use.

## SmartAPI login check

Set the following variables in an untracked `.env` file beside `config.yaml`, using one `NAME=value` per line. Nonempty environment variables take precedence over `.env` values.

| Variable | Value |
| --- | --- |
| `ANGEL_TRADING_API_KEY` | SmartAPI application key |
| `ANGEL_CLIENT_ID` | Angel One client code |
| `ANGEL_PASSWORD` | SmartAPI login PIN, including any leading zeros |
| `ANGEL_TOTP_SECRET` | Base32 authenticator setup secret |
| `ANGEL_ONE_LOCAL_IP` | Optional local IP header |
| `ANGEL_ONE_PUBLIC_IP` | Optional public IP header |
| `ANGEL_ONE_MAC_ADDRESS` | Optional colon-delimited MAC override. OrderBOT detects the adapter MAC when the value is omitted |

Run `python run.py --broker-login` to authenticate. The command reports success or a sanitized error and exits. The [SmartAPI documentation](https://smartapi.angelone.in/docs) describes the request headers and authentication flow.

## Market data and replay

```powershell
python run.py --fetch-instruments
python run.py --capture-data
python run.py --replay YYYY-MM-DD
python run.py --compare-history YYYY-MM-DD
```

- Fetch the public instrument master before the session. The cache uses the IST session date. Capture connects to SmartAPI for data only and requires the environment variables above.
- Start capture during the configured lead-in before the continuous session opens to record the pre-open traded-volume baseline. The futures and spot tokens come from that day's master. Repeat `--option-token TOKEN` to include unexpired NIFTY options.
- The capture writes raw Snap Quote frames into dated Parquet partitions. It stores any historical seed used for indicators alongside them. Replay uses the dated cache and recorded timestamps and works without SmartAPI credentials.
- Run the history comparison after the session closes. It requires a continuous capture, a reference history with the same candle coverage, OHLC within one instrument tick, and exact volume. It reports each mismatched minute and exits with status 1 if the check fails.
- Use the session hours applicable to the chosen contract and date in `config.yaml`. The default is 09:15–15:30 IST. NSE lists a 15:45 close for most index derivatives from 7 December 2026 and a 15:30 close for expiring index contracts on expiry day. [NSE session timing FAQ](https://nsearchives.nseindia.com/web/mediaattachment/2026-11/FAQs_for_CM_and_FO_FOR_CAS_2.0_20261109152618.pdf)
- For a fixture-only run, use `python -m pytest tests/test_data_layer.py tests/test_history.py`. The synthetic session covers 375 one-minute candles and verifies the CLI replay output byte for byte.

## Paper costs and risk

`python -m pytest tests/test_cost_model.py tests/test_position_risk.py` runs the sizing, risk and fee fixtures. Sizing uses the selected contract's lot size and whole lots. If capital or per-trade risk is unset, the risk manager rejects entries. Reduce-only exits remain eligible against a verified held quantity.

The paper charge model rounds each line item half-up to a paisa and adds those line items. It uses executed buy and sell prices for fees and net P&L. The entry gate uses projected fills to account for both spreads and the configured slippage without charging them again in net P&L.

The default exchange rate including IPFT follows §15 of the spec. Angel One's public calculator displays totals reproduced with 0.03552% for the three committed cases. The fixtures record both sets of results. Account-specific estimates and contract-note rounding still need reconciliation. [Angel One brokerage calculator](https://www.angelone.in/calculators/brokerage-calculator)

## Paper option selection

```powershell
python run.py --replay-options tests/fixtures/phase3_session.json
python -m pytest tests/test_options.py
```

The fixture replay produces a JSON audit of every candidate and requote, with full bid/ask depth, local IV and delta where available, rejection reasons, and proposed child quantities. Its lot counts are fixture inputs. Capital and per-trade risk remain unset.

- Expiries come from the instrument master. The resolver excludes monthly dates identified from the three consecutive listed NIFTY futures and requires two trading sessions after today before it will select a weekly. Supply exchange trading-session dates, including holidays and special sessions, as an input. Incomplete calendar or series data blocks selection. [NSE contract specifications](https://betanseapi.nseindia.com/static/products-services/equity-derivatives-contract-specifications)
- The default moneyness policy admits ATM and one ITM strike. Revalidation aborts on any wider spread or a mid move beyond the configured tolerance. It keeps the original contract identity throughout the check.
- Black-76 estimates forward delta from an expiry-matched forward derived from spot and the configured rate and dividend yield. The paper rate, dividend yield, liquidity floors and requote tolerance are assumptions in `config.yaml`. A broker delta, when supplied, serves as a comparison.
- The split plan applies the configured NSE inclusive cap, the broker's raw field as an exclusive ceiling, and whole contract lots. The broker's interpretation of its raw field and actual acceptance of child quantities still need verification. [NSE quantity-freeze report](https://nsearchives.nseindia.com/content/fo/qtyfreeze.csv)

## Paper execution and restart

```powershell
python run.py --replay-execution tests/fixtures/phase4_execution.json
python -m pytest tests/test_execution.py
```

The replay uses fixture funding in isolated temporary databases. The checked-in account settings remain unset. The paper venue fills at the displayed ask for buys and bid for sells, adjusted by configured slippage and bounded by the order limit. It consumes only the displayed quantity at that price and shares that quantity across children. The executor cancels outstanding quantities at the partial-order deadline. Closed paper trades include the full charge stack and net P&L from completed fills.

The journal commits the intent and tag before submitting to the simulated venue. After a timeout it waits and looks up the tag once. An unresolved send retains its pending state and latches a session halt. On startup and at the configured interval when polled, reconciliation compares the independent paper books with the journal, recovers corroborated fills, and alerts on mismatches. The fixture tests also reopen those SQLite files in a new Python process.

Angel One's authenticated order-book behavior, tag visibility after a timeout, partial-fill and cancellation timing, and position-book lag are pending verification. [SmartAPI order documentation](https://smartapi.angelone.in/docs/ResponseStructure)

## Strategy A fixture replay

```powershell
python run.py --replay-strategy-a tests/fixtures/phase5_session.json
python -m pytest tests/test_strategy_a.py
```

The replay calculates VWAP, Wilder RSI and ATR from 375 synthetic near-month futures candles, evaluates all nine entry gates, and routes CE and PE signals through the paper executor. The audit records a result for each gate and the projected and fill-anchored stop plans for completed trades. Fixture events request the exits. The Phase 6 exit engine supplies automated exits.

The entry window applies to the actual decision and submission clocks, with the end instant excluded. The noon cutoff uses the listed market expiry even when the selected option has rolled to a later date. The optional pivot rule uses the directional band in §19. The IV gate uses the last 20 completed session observations for the same option right, the nearest-rank 80th percentile, and a strict below-threshold comparison on the current ATM contract. Collection of authenticated ATM IV samples and execution against recorded Angel One market data still need verification.

## Paper exits and kill-switch drills

```powershell
python run.py --replay-exits tests/fixtures/phase6_exits.json
python -m pytest tests/test_exit_engine.py
```

The exit controller consumes ticks, closed futures candles, and timer or health events. It rebuilds the stop plan from the journal and the actual average entry fill. At +1R it raises the premium stop to the on-tick fee break-even estimate. Above +2R a closed candle can raise the stop to capture half the peak premium gain. Exit priority puts square-off and kill switches first, then the premium floor and the underlying and mapped premium stops before either target.

At 15:10 IST the controller latches the entry halt and runs the bid-referenced limit ladder with configured paper slippage, two wider offsets, and confirmed cancellation before each reprice. The 15:20 backstop uses a paper sell limit protected by a configured fraction of fresh option LTP. This models price protection with visible depth. A failed ladder or a backstop that cannot confirm flat emits a durable alert. §14 reserves protected execution for the backstop, so ordinary exits use the limit ladder and alert on exhaustion.

All nine kill switches latch through SQLite. A switch cancels known working buys before processing further fills and attempts to close the confirmed position. A stale feed can delay the sell until an executable quote arrives. A position mismatch blocks new entries and holds liquidation until the independent position book reconciles. The fixture uses synthetic funding to seed the position and runs exits with capital and risk unset.

Live timer and health-event delivery, Angel One depth, order/cancellation acknowledgement, and the broker's protection bands and fill behavior remain unverified. [Angel One market-price protection policy](https://www.angelone.in/riskpolicy)

## Paper dashboard, report and alerts

```powershell
python run.py --dashboard --surface-fixture tests/fixtures/phase7_surface.json
python run.py --report 2026-11-30 --surface-fixture tests/fixtures/phase7_surface.json --report-format markdown
python -m pytest tests/test_reporting.py
```

The dashboard prints its loopback address at startup. The fixture creates independent temporary paper books and uses a fixed IST clock. Stop trading latches the current session in the journal. A running paper exit engine consumes that latch on its next event and waits for a valid quote if it still holds a position.

Run `python run.py --dashboard` to use the configured journal. The dashboard process writes the dated Markdown report at the configured time, 15:35 IST by default, and catches up for that IST day when started after the cutoff. `python run.py --report YYYY-MM-DD` reads the same journal on demand and returns JSON by default. A reconciliation mismatch or missing fill or charge evidence gives the report command exit status 1. The report totals booked charges and independently recomputes each trade's net P&L from cumulative fill values. Older fills that lack touch-price evidence remain eligible for P&L reconciliation and are counted separately in the slippage comparison.

Alerts for entry, exit, kill-switch and reconciliation failures are durable in SQLite. To route them to Telegram, set `alerts.telegram_enabled` in `config.yaml` and provide `ORDERBOT_TELEGRAM_TOKEN` and `ORDERBOT_TELEGRAM_CHAT_ID` in the dashboard process environment. Message delivery and retries run in the background while the dashboard process is running. A delivery timeout may result in a repeat message. Fixture dashboard sessions always use the local outbox. [Telegram Bot API](https://core.telegram.org/bots/api#sendmessage)

### F&O stock movers

`python run.py --dashboard` also scans the NSE cash price of every stock with listed futures, once a minute during the session, using the SmartAPI quote endpoint and the `.env` credentials. The dashboard shows the top gainers, top losers and advancer/decliner breadth. The "Holding highs" and "Holding lows" tags mark a stock on the trend side of VWAP and in the outer quarter of its day range. They are an untested watchlist aid and no strategy trades on them. The scan is read-only, stops after three failed logins, and runs once when the dashboard starts outside market hours. Fixture dashboards show synthetic quotes from the fixture file.

### Stock intraday paper trading

With `stocks.paper_trading: true` the dashboard process also paper-trades the scanned stocks. It never sends an order. The `stocks` section holds its own capital (₹1,00,000) and risk per trade (0.5%, ₹500), so the option risk inputs stay unset. It reuses the daily loss limit (2%), entry cap, loss-streak cap, square-off time and Stop trading latch from the option settings and uses no leverage.

- **Stocks in play** (`stocks.sip_entries`). At 09:20 it ranks stocks by the volume of their first five minutes against their 14-session average and keeps the top `sip_candidates` at or above `sip_min_relative_volume`. Each is traded once, on a break of its five-minute range in the direction of that range's move, until `strategy_a.entry_close`. The stop is `sip_stop_atr` of the 14-session ATR, there is no target, and the position closes at the stop or the square-off. The rules follow Zarattini, Barbon and Aziz (2024), with the stop widened from 0.1 to 0.5 ATR because the published stop lost money after Indian intraday charges in a 46-session test of 69 F&O stocks. [Paper](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284)
- The 14-session figures come from SmartAPI minute history for every scanned stock, about 220 requests, and are saved for the day under `data.history_cache_dir`. Start the dashboard by about 09:05 so they are ready at 09:20. A later start skips this entry for the day.
- It holds up to `max_positions` positions, each limited to an equal share of the capital, so two positions of ₹50,000.
- Until `strategy_b.entry_window_end` it trades the first opening-range breakout, using the Strategy B rules: open at the range edge, gap agreement, breakout buffer, stop at the opposite edge and a 1.5R target. The range is the day's high and low at the first scan after 09:30, so the dashboard must be running by then. A later start skips this entry for the day.
- The VWAP entry is off by default (`stocks.vwap_entries: false`). In a 60-session backtest of 20 stocks (21/08/2026 to 18/11/2026) it lost money in both the tuning and the validation period while the breakout stayed profitable. When switched on, and if the breakout did not trade, it takes a VWAP crossover in the direction of the day's move with a volume surge and RSI inside the Strategy A bands. The stop and target are the Strategy A ATR multiples. It needs about 22 minutes of scans before the first signal.
- Stops are held between `min_stop_pct` and `max_stop_pct` of price. Entries need a tight spread, enough displayed quantity, and charges under the friction cap. Buys fill at the ask and sells at the bid.
- Charges follow Angel One's published equity intraday rates: brokerage at the lower of ₹20 or 0.1% per order with a ₹5 minimum, STT on the sell side, exchange, SEBI, stamp duty and GST. [Angel One charges](https://www.angelone.in/exchange-transaction-charges)

The dashboard has three screens: Stocks, Options and Backtest.

- **Manual paper trades.** On the Stocks screen, type an F&O stock symbol and press Buy or Sell short. The trade uses the last scanned quote, the same risk sizing, a stop of at least `min_stop_pct`, a 2R target and the automatic exits. Close ends it at the current quote. The Stop trading latch, square-off time, daily loss limit and one-position rule still apply.
- **Forward test.** The accuracy table counts every closed paper trade in the journal per strategy: trades, wins, accuracy, average win and loss, profit factor and net P&L after charges.
- **P&L chart and trade view.** The Stocks screen charts the running paper P&L by strategy. Clicking a stock in today's trades opens that session's minute prices with the entry, stop, target and exit marked; `#trade-ID` in the address opens one directly.
- **Extra exits and watchlist.** `exit_on_rsi` and `exit_on_vwap_cross` close a position on the minute scan when RSI is stretched in its favour or price is back through VWAP. The pre-market watchlist scores each stock on ATR, last-session volume and five-session move; `watchlist_only` limits entries to it. All three switches are off: none was profitable in both periods of the 60-session replay.
- **Resume trading.** After the dashboard's Stop trading, Resume trading clears that day's latch. Other kill switches stay latched.
- **Summary and position cards.** The Stocks screen totals today, this week and this month, and shows each open position with its live P&L, time held and how much of the stop distance is left.
- **Stocks in play panel.** Today's candidates with their breakout level, opening volume against usual, and whether each is waiting, traded or broke out without a trade.
- **Settings screen.** Capital, risk per trade (at most 2%), positions at once and the strategy and exit switches can be changed from the dashboard after a confirmation. Choices are saved in `stock-settings.json` beside the journal and applied over `config.yaml`; open positions keep their size and stop.
- **Backtest history.** The last 20 backtest runs are kept in `backtest-history.json` beside the journal with their settings, so runs can be compared.
- **Tables, alerts and explanations.** Every table sorts on a header click and the longer ones have a search box. Trade alerts (a browser notification and a tone when a paper trade opens or closes) are switched on per browser from the top bar. When there is no stock trade yet, the Stocks screen lists the reasons in plain words, such as a late start or a pressed Stop trading. The layout stacks into one column on narrow screens.
- **Broker link.** The top bar shows the stock scanner's broker session, including "market closed" outside the session, and has a Connect / Disconnect button. Disconnect stops price scans and paper trading until Connect, and is refused while a paper position is open. Connect logs in again and scans once at any hour. After three failed logins the link pauses itself until Connect is pressed.
- **NIFTY option prices.** With each minute scan the Options screen shows NIFTY spot and the nearest expiry's calls and puts for six strikes either side of the money: price, change, open interest, the at-the-money call-plus-put price and the put/call open-interest ratio. It is for viewing only. The paper-engine panels below it fill only when an options paper run writes to the journal.
- **Backtest.** The Backtest screen replays up to 25 stocks over up to 31 days of SmartAPI one-minute candles through the same trader code, in a temporary journal. Each candle close stands in for the minute quote with a one-tick spread. Past sessions are cached under `data.history_cache_dir`.

Between the minute scans, open positions are re-quoted every five seconds and closed at the stop or target, so an exit fills within a few seconds of the level. The backtest mirrors this by checking each later candle's high and low. Entries and indicators still use the minute scan. A NIFTY direction filter (longs only while NIFTY is above its open) was tested over the same 60 sessions and left out: it helped the first 39 sessions and hurt the last 21.

Entry prices are sampled once a minute, so an entry fills at the next scan's quote, and RSI, ATR and volume come from those samples. Results are an estimate until the trader moves to the tick feed. The daily report does not include stock trades. `python -m pytest tests/test_stock_paper.py` checks sizing, exits and charges to the paisa.

## Strategy B and end-to-end paper fixtures

```powershell
python run.py --replay-paper-session tests/fixtures/phase8_session.json --strategy ohl
python run.py --replay-paper-session tests/fixtures/phase8_session.json --strategy ohl --fixture-funding
python run.py --replay-paper-session tests/fixtures/phase8_session.json --strategy ohl --fixture-funding --fixture-scenario no_ohl_trade
```

Strategy B builds the opening range from closed NIFTY near-month futures candles and freezes it at 09:30 IST with the default settings. The open must be near a range edge and the previous futures close must confirm the gap direction. The first closed candle that clears the tagged breakout by one futures tick plus the configured buffer can enter through the same option selection, liquidity, and risk pipeline used by Strategy A. The stop uses the closer of the opposite range edge and the ATR distance, with a 1.5R target by default.

`ohl` mode gives Strategy B the morning window. At 10:00 IST, working OHL buys are cancelled and Strategy A becomes eligible only if there was no OHL fill that session. The journal preserves this rule after restart, even when the OHL position is already closed. `vwap` mode runs Strategy A only.

The replay writes synthetic Snap Quote packets to a temporary Parquet archive and drives both strategies from the replayed session. The funded runs use fixture-only capital and risk to exercise fills, exits, reconciliation, the net daily report, and queued alerts. The default unfunded run exercises the risk veto. These commands require no credentials or network access. Section 18 Phase 9 calls for 30 actual paper sessions, which are still pending.

## Real-data paper trial

Run `python run.py --mode paper --real-paper-trial PATH_TO_NSE_FO_CALENDAR.json` during the configured session. The calendar contains an ascending `trading_sessions` array of exchange dates covering the prior session through the candidate weekly expiry. It can include `source` for the NSE circular. The trial uses the configured strategy and requires both risk inputs to be unset.

The trial evaluates five minutes by default in an isolated journal. It accepts a signal candle after the post-close delay and two agreeing SmartAPI history responses. Fresh spot and option books come from the WebSocket. The risk gate blocks otherwise eligible entries with the account inputs unset. The output directory contains the raw tick archive and `report.json` with signal decisions, a later check for broker revisions, and an independent WebSocket candle comparison. The comparison converts the NIFTY futures history volume from lots to units using the listed contract size and applies the instrument tick tolerance to prices.

Add `--through-close` to keep the WebSocket open to the configured close and allow the final minute to confirm afterwards. `--session-date YYYY-MM-DD` waits for the configured pre-open lead time on an exchange session included in the calendar. A mid-session start reports the earlier minutes as history-only. `report.json` includes periodic progress and separately records API errors, candle mismatches, and live coverage.
