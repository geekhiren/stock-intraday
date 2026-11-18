from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import count
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from orderbot.broker.instruments import InstrumentMaster, parse_master
from orderbot.broker.ws_client import Level
from orderbot.core.config import Config, ExecutionConfig, RiskConfig
from orderbot.core.ist_time import IST, parse_timestamp
from orderbot.core.money import round_inr
from orderbot.data.candle_builder import MINUTE, Candle, TradingSession
from orderbot.data.indicators import IndicatorEngine
from orderbot.data.replay import AnalyzedBar
from orderbot.execution.journal import Journal
from orderbot.execution.paper_executor import PaperExecutor
from orderbot.execution.paper_venue import PaperVenue
from orderbot.options.liquidity_gate import OptionQuote

from .iv_store import IVStore
from .runner import submit_strategy_decision
from .vwap_rsi import StrategyA, StrategyDecision


@dataclass(frozen=True, slots=True)
class QuoteEvent:
    quote: OptionQuote
    spot: Decimal
    fixture_exit: str | None


@dataclass(frozen=True, slots=True)
class StrategyFixture:
    config: Config
    session: TradingSession
    master: InstrumentMaster
    trading_sessions: tuple[date, ...]
    future_token: str
    candles: tuple[Candle, ...]
    events: tuple[QuoteEvent, ...]
    prior_iv: tuple[tuple[date, Decimal], ...]


def load_strategy_fixture(path: Path, config: Config) -> StrategyFixture:
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
        master_rows = json.loads(path.with_name(fixture["instrument_master"]).read_text(encoding="utf-8"))
        master = parse_master(master_rows, config.market.underlying)
        calendar = json.loads(path.with_name(fixture["trading_calendar_fixture"]).read_text(encoding="utf-8"))
        sessions = tuple(date.fromisoformat(item) for item in calendar["trading_sessions"])
        session = TradingSession.from_config(date.fromisoformat(fixture["date"]), config.market)
        if session.day not in sessions:
            raise ValueError("The fixture session must be in the supplied trading calendar.")
        candles = tuple(
            Candle(session.opens_at + index * MINUTE, Decimal(o), Decimal(h), Decimal(l), Decimal(c), volume)
            for index, (o, h, l, c, volume) in enumerate(fixture["futures_ohlcv"])
        )
        if len(candles) != (session.closes_at - session.opens_at) // MINUTE:
            raise ValueError("The strategy fixture must contain every configured session minute.")
        events = []
        valid_tokens = {item.token for item in master.options}
        for row in fixture["quotes"]:
            at = parse_timestamp(row["at"])
            if (
                row["token"] not in valid_tokens or not session.opens_at <= at < session.closes_at
                or (events and events[-1].quote.received_at > at)
            ):
                raise ValueError("Fixture quotes must be ordered, in-session and match listed options.")
            bids, asks = (
                tuple(Level(Decimal(price), quantity, orders) for price, quantity, orders in row[side])
                for side in ("bids", "asks")
            )
            quote = OptionQuote(
                row["token"], at, at, bids, asks, config.options.min_open_interest, config.options.min_day_volume,
            )
            events.append(QuoteEvent(quote, Decimal(row["spot"]), row.get("fixture_exit")))
        risk = RiskConfig.model_validate(config.risk.model_dump() | fixture["funding"])
        execution = ExecutionConfig.model_validate(
            config.execution.model_dump() | {"paper_rejection_pct": fixture["paper_rejection_pct"]},
        )
        prior = tuple((date.fromisoformat(day), Decimal(value)) for day, value in fixture["previous_atm_iv"])
        if any(day >= session.day for day, _ in prior):
            raise ValueError("Fixture ATM IV baseline must precede the replay session.")
        return StrategyFixture(
            config.model_copy(update={"risk": risk, "execution": execution}), session, master, sessions,
            master.front_future(session.day).token, candles, tuple(events), prior,
        )
    except (KeyError, TypeError, ArithmeticError):
        raise ValueError("Invalid Strategy A fixture.") from None


def replay_strategy_a(path: Path, config: Config) -> dict:
    fixture = load_strategy_fixture(path, config)
    cfg = fixture.config
    at = fixture.session.opens_at
    identifiers = count()
    contracts = {item.token: item for item in fixture.master.options}
    history: list[AnalyzedBar] = []
    decisions: dict[str, StrategyDecision] = {}
    audits, exits, alerts = [], {}, []
    quotes: dict[str, OptionQuote] = {}
    last_spot: tuple[datetime, Decimal] | None = None
    events = iter(fixture.events)
    event = next(events, None)

    def advance(seconds):
        nonlocal at
        at += timedelta(seconds=seconds)

    with TemporaryDirectory(prefix="orderbot-phase5-") as directory:
        journal = Journal(Path(directory) / "strategy-journal.sqlite3", cfg.costs)
        history_iv = IVStore(journal.path)
        for day, value in fixture.prior_iv:
            observed = datetime.combine(day, cfg.market.session_close, tzinfo=IST)
            for right in ("CE", "PE"):
                history_iv.record(right, observed, value)
        executor = PaperExecutor(
            journal, PaperVenue(Path(directory) / "strategy-venue.sqlite3", cfg), cfg,
            clock=lambda: at, sleep=advance, tag_factory=lambda: f"strata{next(identifiers):06}",
            alert=alerts.append,
        )
        strategy = StrategyA(cfg, fixture.master, fixture.trading_sessions, history_iv)
        indicators = IndicatorEngine(cfg.data.rsi_period, cfg.data.atr_period)
        for candle in fixture.candles:
            decision_at = candle.at + MINUTE + timedelta(seconds=cfg.data.close_grace_seconds)
            while event is not None and event.quote.received_at <= decision_at:
                at = event.quote.received_at
                quotes[event.quote.token] = event.quote
                last_spot = at, event.spot
                executor.poll(quotes)
                if event.fixture_exit is not None:
                    position = next((item for item in executor.positions() if item.token == event.quote.token), None)
                    if position is None:
                        raise ValueError("Fixture exit has no matching paper position.")
                    outcome = executor.exit_position(
                        position.decision_id, contracts[position.token], event.quote, reason=event.fixture_exit,
                    )
                    if outcome.status != "submitted":
                        raise ValueError(f"Fixture exit failed ({outcome.reason}).")
                    exits[position.decision_id] = event.fixture_exit
                event = next(events, None)

            at = decision_at
            executor.poll(quotes)
            row = AnalyzedBar(candle, indicators.update(candle))
            spot = last_spot[1] if last_spot and (
                at - last_spot[0]
            ).total_seconds() < cfg.options.quote_max_age_seconds else None
            decision = strategy.evaluate(row, history, at=at, token=fixture.future_token, spot=spot, quotes=quotes)
            history.append(row)
            outcome = submit_strategy_decision(decision, executor, spot=spot, quotes=quotes)
            audits.append(decision.view() | {"execution": asdict(outcome)})
            if decision.signal is not None:
                decisions[decision.id] = decision

        ledger = journal.snapshot()
        trades = []
        for trade in ledger.trades:
            decision = decisions[trade["trade_id"]]
            signal = decision.signal
            trade = trade | {
                "right": signal.right, "side": signal.side, "exit_reason": exits[decision.id],
                "signal_candle_at": decision.candle_at.isoformat(),
                "fill_anchored_stop_plan": signal.stop_plan.with_entry_fill(Decimal(trade["buy_average"])).view(),
            }
            trades.append(trade)
        return {
            "mode": "paper", "source": "synthetic_closed_futures", "session": fixture.session.day.isoformat(),
            "risk_inputs_in_config": config.risk.capital_inr is not None and config.risk.risk_per_trade_pct is not None,
            "future_token": fixture.future_token, "closed_candles": len(history),
            "decisions": audits, "trades": trades, "open_positions": [item.view() for item in ledger.positions],
            "net_pnl_inr": str(round_inr(sum((Decimal(item["net_pnl_inr"]) for item in trades), Decimal(0)))),
            "alerts": alerts,
        }
