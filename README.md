# OrderBOT

OrderBOT uses Python 3.11+ and runs in paper mode. The command-line entry point initializes storage, checks SmartAPI authentication, and captures or replays market data for the NIFTY near-month futures signal series.

```powershell
python -m pip install -e ".[test]"
python run.py --check-config
python run.py
python -m pytest
```

`config.yaml` leaves capital and per-trade risk unset. Relative data and log paths resolve beside the YAML file. SQLite timestamps use explicit UTC offsets. The JSON logs use IST.

## SmartAPI login check

Set the following variables in the environment from which you run OrderBOT.

| Variable | Value |
| --- | --- |
| `ANGEL_ONE_API_KEY` | SmartAPI application key |
| `ANGEL_ONE_CLIENT_CODE` | Angel One client code |
| `ANGEL_ONE_MPIN` | SmartAPI login PIN, including any leading zeros |
| `ANGEL_ONE_TOTP_SECRET` | Base32 authenticator setup secret |
| `ANGEL_ONE_LOCAL_IP` | Local IP of the client machine |
| `ANGEL_ONE_PUBLIC_IP` | Public egress IP used with SmartAPI |
| `ANGEL_ONE_MAC_ADDRESS` | Colon-delimited MAC of the client machine |

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
