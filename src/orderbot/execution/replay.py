from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import count
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from orderbot.broker.instruments import Instrument, parse_master
from orderbot.broker.ws_client import Level
from orderbot.core.config import Config, ExecutionConfig, RiskConfig
from orderbot.core.ist_time import parse_timestamp
from orderbot.options.liquidity_gate import OptionQuote
from orderbot.options.strike_selector import StrikeDecision, select_strike

from .journal import Journal
from .paper_executor import PaperExecutor
from .paper_venue import PaperVenue


@dataclass(frozen=True, slots=True)
class ExecutionFixture:
    config: Config
    contract: Instrument
    at: datetime
    spot: Decimal
    premium_stop: Decimal
    target_mid: Decimal
    target_bid: Decimal
    entry: OptionQuote
    exit: dict
    selection: StrikeDecision
    expected: dict


def _quote(token: str, at: datetime, book: dict, config: Config) -> OptionQuote:
    def levels(side):
        return tuple(Level(Decimal(price), qty, orders) for price, qty, orders in book[side])

    return OptionQuote(
        token, at, at, levels("bids"), levels("asks"),
        config.options.min_open_interest, config.options.min_day_volume,
    )


def load_execution_fixture(path: Path, config: Config) -> ExecutionFixture:
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
        master = parse_master(
            json.loads(path.with_name(fixture["instrument_master"]).read_text(encoding="utf-8")),
            config.market.underlying,
        )
        calendar = json.loads(path.with_name(fixture["trading_calendar_fixture"]).read_text(encoding="utf-8"))
        sessions = tuple(date.fromisoformat(day) for day in calendar["trading_sessions"])
        contract = next(item for item in master.options if item.token == fixture["instrument_token"])
        at = parse_timestamp(fixture["at"])
        spot = Decimal(fixture["spot"])
        entry = _quote(contract.token, at, fixture["entry"], config)
        selection = select_strike(
            master, sessions, {contract.token: entry}, at=at, spot=spot, right=contract.right,
            lots=1, config=config.options,
        )
        if selection.selected_token != contract.token:
            raise ValueError("The entry fixture did not produce a selectable contract.")
        risk = RiskConfig.model_validate(config.risk.model_dump() | fixture["funding"])
        execution = ExecutionConfig.model_validate(
            config.execution.model_dump() | {"paper_rejection_pct": fixture["paper_rejection_pct"]},
        )
        return ExecutionFixture(
            config.model_copy(update={"risk": risk, "execution": execution}), contract, at, spot,
            Decimal(fixture["premium_stop"]), Decimal(fixture["target_mid"]), Decimal(fixture["target_bid"]),
            entry, fixture["exit"], selection, fixture.get("expected", {}),
        )
    except (KeyError, TypeError, StopIteration, ArithmeticError):
        raise ValueError("Invalid execution fixture.") from None


def replay_execution(path: Path, config: Config) -> dict:
    fixture = load_execution_fixture(path, config)
    at = fixture.at
    calls, waits, alerts = [], [], []
    ids = count()

    def advance(seconds):
        nonlocal at
        waits.append(seconds)
        at += timedelta(seconds=seconds)

    class TimeoutAfterAcceptance(PaperVenue):
        def submit(self, intent, quote, at):
            calls.append("submit")
            super().submit(intent, quote, at)
            raise TimeoutError

        def order_book(self):
            calls.append("order_book")
            return super().order_book()

    with TemporaryDirectory(prefix="orderbot-phase4-") as directory:
        journal_path = Path(directory) / "journal.sqlite3"
        venue_path = Path(directory) / "paper-venue.sqlite3"
        initial = PaperExecutor(
            Journal(journal_path, config.costs), TimeoutAfterAcceptance(venue_path, fixture.config), fixture.config,
            clock=lambda: at, sleep=advance, tag_factory=lambda: f"replay{next(ids):04}", alert=alerts.append,
        )
        entered = initial.enter(
            "fixture-signal", "vwap", fixture.selection, fixture.entry, spot=fixture.spot,
            premium_stop=fixture.premium_stop, target_mid=fixture.target_mid, target_bid=fixture.target_bid,
        )
        if entered.status != "submitted":
            raise ValueError(f"Execution fixture entry failed ({entered.reason}).")

        journal = Journal(journal_path, config.costs)
        restarted = PaperExecutor(
            journal, PaperVenue(venue_path, fixture.config), fixture.config,
            clock=lambda: at, sleep=advance, tag_factory=lambda: f"replay{next(ids):04}", alert=alerts.append,
        )
        reconciled = restarted.reconcile()
        if not reconciled.ok:
            raise ValueError(f"Execution fixture reconciliation failed ({reconciled.reason}).")
        restored = [position.view() for position in reconciled.positions]
        at = fixture.at + timedelta(seconds=fixture.config.execution.partial_timeout_seconds)
        restarted.poll({})
        exit_quote = _quote(fixture.contract.token, at, fixture.exit, fixture.config)
        exited = restarted.exit_position("fixture-signal", fixture.contract, exit_quote, reason="fixture_exit")
        if exited.status != "submitted":
            raise ValueError(f"Execution fixture exit failed ({exited.reason}).")
        return {
            "mode": "paper", "source": "fixture",
            "risk_inputs_in_config": config.risk.capital_inr is not None and config.risk.risk_per_trade_pct is not None,
            "timeout": {"calls": calls, "wait_seconds": waits},
            "positions_after_restart": restored,
            "positions_after_exit": [position.view() for position in restarted.positions()],
            "orders": {tag: journal.history(tag) for tag in entered.order_tags + exited.order_tags},
            "trades": list(journal.snapshot().trades), "alerts": alerts,
        }
