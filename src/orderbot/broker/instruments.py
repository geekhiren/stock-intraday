from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests

from orderbot.core.atomic_file import atomic_path
from orderbot.core.config import DataConfig


SCRIP_MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
NSE_CASH = 1
NSE_DERIVATIVES = 2


class InstrumentError(ValueError):
    """Invalid or missing broker instrument metadata."""


@dataclass(frozen=True, slots=True)
class Instrument:
    token: str
    symbol: str
    exchange: str
    expiry: date | None
    strike: Decimal | None
    right: str | None
    lot_size: int
    price_tick: Decimal
    raw_freeze_quantity: int | None


@dataclass(frozen=True, slots=True)
class InstrumentMaster:
    spot: Instrument
    futures: tuple[Instrument, ...]
    options: tuple[Instrument, ...]

    def front_future(self, day: date) -> Instrument:
        for future in self.futures:
            if future.expiry >= day:
                return future
        raise InstrumentError("No unexpired NIFTY future in the dated instrument master.")


def _parse_derivative(row: dict, underlying: str) -> Instrument:
    expiry_match = re.fullmatch(r"([0-9]{2})([A-Z]{3})([0-9]{4})", row["expiry"])
    token, symbol = row["token"], row["symbol"]
    if not expiry_match or not re.fullmatch(r"[0-9]+", token):
        raise InstrumentError("Unexpected derivative expiry or token format in the instrument master.")
    day, month, year = expiry_match.groups()
    expiry = date(int(year), SCRIP_MONTHS.index(month) + 1, int(day))
    symbol_prefix = underlying + day + month + year[-2:]
    tick = Decimal(row["tick_size"]) / 100
    lot = int(row["lotsize"])
    raw_freeze = int(row["freeze_qty"]) if row.get("freeze_qty") not in (None, "") else None
    if not tick.is_finite() or tick <= 0 or lot <= 0 or (raw_freeze is not None and raw_freeze <= 0):
        raise InstrumentError(f"Invalid tick, lot size or freeze field for {symbol}.")
    strike = right = None
    if row["instrumenttype"] == "OPTIDX":
        option = re.fullmatch(re.escape(symbol_prefix) + r"([0-9]+(?:\.[0-9]+)?)(CE|PE)", symbol)
        if not option:
            raise InstrumentError(f"Unexpected option symbol or expiry format for {symbol}.")
        strike = Decimal(row["strike"]) / 100
        if not strike.is_finite() or strike <= 0 or strike != Decimal(option[1]):
            raise InstrumentError(f"Option symbol and scaled strike disagree for {symbol}.")
        right = option[2]
    elif symbol != symbol_prefix + "FUT":
        raise InstrumentError(f"Unexpected futures symbol or expiry format for {symbol}.")
    return Instrument(token, symbol, row["exch_seg"], expiry, strike, right, lot, tick, raw_freeze)


def parse_master(rows, underlying: str) -> InstrumentMaster:
    if not isinstance(rows, list):
        raise InstrumentError("Instrument master must contain an array of contracts.")
    futures, options, spots = [], [], []
    tokens = set()
    try:
        for row in rows:
            if row.get("name") != underlying:
                continue
            exchange, kind = row.get("exch_seg"), row.get("instrumenttype")
            if exchange == "NFO" and kind in {"FUTIDX", "OPTIDX"}:
                contract = _parse_derivative(row, underlying)
                if contract.token in tokens:
                    raise InstrumentError("Duplicate derivative token in the instrument master.")
                tokens.add(contract.token)
                (futures if kind == "FUTIDX" else options).append(contract)
            if exchange == "NSE" and kind == "AMXIDX" and row.get("symbol") == "Nifty 50":
                if not re.fullmatch(r"[0-9]+", row["token"]):
                    raise InstrumentError("Unexpected NIFTY spot token format.")
                spots.append(Instrument(row["token"], row["symbol"], exchange, None, None, None,
                                        int(row["lotsize"]), Decimal(0), None))
    except InstrumentError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError, InvalidOperation):
        raise InstrumentError("Malformed instrument master contract.") from None
    if not futures or not options or len(spots) != 1:
        raise InstrumentError("Instrument master is missing NIFTY futures, options or the spot index.")
    future_dates = {future.expiry for future in futures}
    if len(future_dates) != len(futures):
        raise InstrumentError("Instrument master has multiple NIFTY futures for one expiry.")
    return InstrumentMaster(
        spot=spots[0],
        futures=tuple(sorted(futures, key=lambda contract: contract.expiry)),
        options=tuple(sorted(options, key=lambda contract: (contract.expiry, contract.strike, contract.right))),
    )


def load_master(
    config: DataConfig, underlying: str, day: date, directory: Path, *,
    download: bool = True, transport=requests,
) -> InstrumentMaster:
    target = directory / config.instrument_cache_dir / f"instruments-{day.isoformat()}.json"
    if target.is_file():
        try:
            return parse_master(json.loads(target.read_text(encoding="utf-8")), underlying)
        except (json.JSONDecodeError, UnicodeError):
            raise InstrumentError(f"Unreadable cached instrument master for {day}.") from None
    if not download:
        raise InstrumentError(f"No cached instrument master for {day}.")
    try:
        response = transport.get(
            config.instrument_master_url,
            timeout=config.instrument_download_timeout_seconds,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise InstrumentError(f"Instrument master download returned HTTP {response.status_code}.")
        rows = response.json()
    except InstrumentError:
        raise
    except (requests.RequestException, ValueError):
        raise InstrumentError("Unable to download or decode the Angel One instrument master.") from None
    master = parse_master(rows, underlying)
    with atomic_path(target) as temporary:
        temporary.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
    return master
