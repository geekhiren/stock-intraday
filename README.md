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
