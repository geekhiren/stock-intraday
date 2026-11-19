from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path

from orderbot.broker.instruments import parse_master
from orderbot.broker.ws_client import Level
from orderbot.core.config import Config
from orderbot.core.feed_status import record_feed_status
from orderbot.core.ist_time import parse_timestamp
from orderbot.core.state_store import open_store
from orderbot.execution.exit_store import ExitProgress, ExitStore
from orderbot.execution.journal import Journal
from orderbot.execution.lifecycle import OrderIntent, OrderState
from orderbot.execution.paper_venue import PaperVenue
from orderbot.options.liquidity_gate import OptionQuote
from orderbot.strategy.stop_plan import build_stop_plan


def seed_surface_fixture(path: Path, config: Config, db: Path, *, venue_db: Path | None = None) -> datetime:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    master_path = path.with_name(fixture["instrument_master"])
    master = parse_master(json.loads(master_path.read_text(encoding="utf-8")), config.market.underlying)
    contracts = {contract.token: contract for contract in master.options}
    with open_store(db) as connection:
        if connection.execute("SELECT 1 FROM decisions LIMIT 1").fetchone() is not None:
            raise ValueError("The dashboard fixture requires an empty journal.")
    now = parse_timestamp(fixture["clock"])
    journal = Journal(db, config.costs)
    venue = PaperVenue(venue_db or db.with_name(f"{db.stem}-venue.sqlite3"), config)
    open_trade = fixture["position"]
    plan = build_stop_plan("long", Decimal(open_trade["underlying_entry"]), Decimal(open_trade["atr"]),
                           Decimal(open_trade["delta"]), Decimal(open_trade["buy_fill"]), config.strategy_a)

    def book(row, index, side, detail=None):
        buy = side == "BUY"
        prefix = "buy" if buy else "sell"
        contract = contracts[row["token"]]
        if contract.symbol != row["symbol"]:
            raise ValueError("Fixture symbol disagrees with its instrument master.")
        stamp = parse_timestamp(row["entry_at" if buy else "exit_at"])
        price, touch = Decimal(row[prefix + "_fill"]), Decimal(row[prefix + "_touch"])
        units = row["quantity"]
        default_stop = price * (1 - config.strategy_a.max_premium_loss_fraction)
        intent = OrderIntent(
            f"surface{index}{side}", row["id"], "vwap", contract.token, contract.symbol, side, units,
            contract.lot_size, contract.price_tick, price, stamp,
            Decimal(row.get("premium_stop", default_stop)) if buy else None,
            "fixture_entry" if buy else row["exit_reason"],
        )
        journal.prepare((intent,), journal.snapshot().revision, decision_detail=detail)
        journal.claim(intent.tag, stamp)
        bid = touch - contract.price_tick if buy else touch
        ask = touch if buy else touch + contract.price_tick
        quote = OptionQuote(contract.token, stamp, stamp, (Level(bid, units, 1),), (Level(ask, units, 1),), 0, 0)
        fill = venue.submit(intent, quote, stamp)
        if fill.state is not OrderState.FILLED or fill.average_price != price:
            raise ValueError("Surface fixture did not produce its expected paper fill. Check the execution and cost settings.")
        journal.sync((fill,), stamp)

    for index, trade in enumerate(fixture["trades"]):
        book(trade, index, "BUY")
        book(trade, index, "SELL")
    book(open_trade, len(fixture["trades"]), "BUY", {"execution_stop_plan": plan.view()})
    ExitStore(db).save(open_trade["id"], ExitProgress(
        last_ltp=Decimal(open_trade["ltp"]), last_tick_at=parse_timestamp(open_trade["mark_at"]),
    ))
    for index, (identifier, stamp, reason) in enumerate(fixture["skips"], start=1):
        detail = {}
        if index == len(fixture["skips"]):
            detail = {"candle_at": parse_timestamp(fixture["candle_at"]).isoformat(),
                      "gates": {key: {"status": value} for key, value in fixture["gates"].items()}}
        journal.record_skip(identifier, "vwap", parse_timestamp(stamp), reason, detail)
    record_feed_status(db, "simulated", now)
    return now
