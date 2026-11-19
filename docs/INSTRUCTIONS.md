# Setup and operations

## Start the paper dashboard

From the repository root, use Python 3.11 or higher.

```powershell
python -m pip install -e ".[test]"
python run.py --check-config
python -m pytest -q
python run.py --dashboard --mode paper
```

Open the [local dashboard](http://127.0.0.1:18765/). Start one dashboard per workspace and port. To upgrade an active instance, check open paper positions before stopping and restarting it. Keep both `risk` and `stocks` capital and risk values unset for data-only verification. Settings saved in the dashboard can override the stock budget, so confirm the dashboard displays both stock values as unset.

Set `ANGEL_TRADING_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PASSWORD` and `ANGEL_TOTP_SECRET` in the local `.env` beside `config.yaml`. Keep the PIN’s leading zeros. Optional network header names are in [the main README](../README.md). The repository ignores `.env` and workspace runtime files under `var/`. The default candle store lives outside the checkout.

## Use local stock candles

The default candle store is `%USERPROFILE%\.orderbot-research\candles`. Set `ORDERBOT_CANDLE_STORE` before launching OrderBOT to point it to a different store. Historical data is grouped by symbol and month.

```powershell
python -m orderbot.data.candle_store status
python -m orderbot.data.candle_store import 'C:\path\to\downloaded-candles'
```

The importer accepts a JSON mapping of stock symbol to candle rows, or a folder with one subdirectory per symbol containing gzip JSON pieces. Each row contains `[timestamp, open, high, low, close, volume]`. Use minute timestamps with explicit offsets and integer volume. The stock reader excludes the closing auction and post-close candles and reuses covered windows. SmartAPI results for uncovered windows are cached in `data.history_cache_dir`. Background baseline fetching is disabled by default. Research runs read local data by default and accept `--fetch-missing` when broker history is explicitly needed. Use the instrument’s exact symbol when comparing derivative candles with a local source.

## Check a regular NSE session

The dashboard separates cash and F&O timings. Under the August 2026 rules, covered cash stocks trade continuously from 09:15 to 15:15 IST, then enter the auction. Equity F&O runs to 15:40. From 7 December 2026, covered cash stocks run continuously to 15:30 and equity F&O generally runs to 15:45. Expiring index derivatives stop at 15:30 on their expiry date. Cash strategies keep their existing square-off time. Special and shortened sessions require separate calendar verification. [August rules](https://betanseapi.nseindia.com/static/products-services/closing-auction-session) · [December rules](https://nsearchives.nseindia.com/web/mediaattachment/2026-11/FAQs_for_CM_and_FO_FOR_CAS_2.0_20261109152618.pdf)

Provide a JSON calendar with an ordered `trading_sessions` array of ISO dates and an optional `source` URL. Keep the paper dashboard running, then launch the verification command during a regular session. An explicit future `--session-date` waits until the opening candle sample is available.

```powershell
python run.py --verify-market-data 'C:\path\to\nse-calendar.json'
python run.py --verify-market-data 'C:\path\to\nse-calendar.json' --session-date YYYY-MM-DD
```

The probe uses the dashboard's session and makes two quote reads and up to two history reads. It checks a cash stock, NIFTY spot, the near-month future and the two nearest at-the-money option legs. The closing delay excludes unfinished candles. The JSON report is saved under `paper_trial.output_directory`. A `local_overlap` of zero means no same-minute local comparison was possible. Use the existing real-data trial to compare a broader option-session capture. [SmartAPI market data](https://smartapi.angelone.in/docs/MarketData)

| Symptom | Check |
| --- | --- |
| Stale quote | Inspect the exchange timestamp and session phase shown next to the quote. The dashboard’s display threshold is `reporting.rest_quote_max_age_seconds`. Stock paper fills use the stricter `stocks.max_quote_age_seconds`. |
| Market closed or closing auction | The screen can show the last snapshot. Cash strategies wait for continuous trading. |
| SmartAPI cooldown | Let the shared workspace limiter honor the broker’s `Retry-After`. The REST default is one request per second and at most 50 tokens per quote batch. |
| Empty candle history | Check the local store path, symbol, requested session, and coverage. A successful broker HTTP response can still contain missing candles. |
| Strategy blocked | Check the saved paper budget, Stop trading state, quote freshness, and gate or rejection reason in the dashboard. |

Use `python run.py --replay-paper-session tests/fixtures/phase8_session.json --strategy ohl --fixture-funding` to exercise paper fills offline with synthetic funding. The checkout’s default budgets remain unset.
