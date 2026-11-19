from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import count
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from orderbot.broker.ws_client import Level
from orderbot.core.config import Config
from orderbot.core.ist_time import parse_timestamp
from orderbot.options.liquidity_gate import OptionQuote
from orderbot.options.strike_selector import revalidate_selection
from orderbot.risk.kill_switch import Health
from orderbot.strategy.stop_plan import build_stop_plan

from .exit_engine import ExitEngine
from .exit_rules import ExitFrame
from .journal import Journal
from .paper_executor import PaperExecutor
from .paper_venue import PaperVenue
from .replay import ExecutionFixture, load_execution_fixture


class ReplayVenue(PaperVenue):
    mismatch = False

    def snapshot(self):
        book = super().snapshot()
        return replace(book, positions={}) if self.mismatch else book


@dataclass
class Drill:
    fixture: ExecutionFixture
    funded: PaperExecutor
    engine: ExitEngine
    venue: ReplayVenue
    clock: list[datetime]
    alerts: list[dict]

    def book(self, data: dict, *, depth: int | None = None, ask: Decimal | None = None) -> OptionQuote:
        now = self.clock[0]
        units = data["depth"] if depth is None else depth
        return replace(
            self.fixture.entry, exchange_at=now, received_at=now,
            bids=(Level(Decimal(data["bid"]), units, 1),),
            asks=(Level(Decimal(data["ask"]) if ask is None else ask, units, 1),),
        )

    def step(self, at: datetime, *, quote: OptionQuote | None = None, health: Health | None = None,
             option_ltp: Decimal | None = None, underlying: Decimal | None = None) -> bool:
        self.clock[0] = at
        kind = "tick" if option_ltp is not None or underlying is not None else "timer"
        return self.engine.step(
            ExitFrame(at, kind, underlying=underlying, option_ltp=option_ltp,
                      option_token=self.fixture.contract.token if option_ltp is not None else None),
            {quote.token: quote} if quote is not None else {},
            health if health is not None else Health(last_option_tick_at=at),
        )

    def try_entry(self, quote: OptionQuote) -> str:
        # Use a new selection clock to exercise the halt independently of unset account inputs.
        at, selected = self.clock[0], self.fixture.selection
        refreshed = revalidate_selection(selected, quote, at=at, spot=self.fixture.spot,
                                         config=self.funded.config.options)
        if refreshed.outcome != "revalidated":
            raise ValueError("Replay post-halt quote must be a valid requote.")
        selected = replace(refreshed, outcome="selected")
        outcome = self.funded.enter(
            "post-halt", "vwap", selected, quote, spot=self.fixture.spot,
            premium_stop=self.fixture.premium_stop, target_mid=self.fixture.target_mid, target_bid=self.fixture.target_bid,
        )
        return outcome.reason


def _open_drill(folder: Path, fixture: ExecutionFixture, config: Config, raw: dict) -> Drill:
    clock, alerts, ids = [fixture.at], [], count()
    journal = Journal(folder / "journal.sqlite3", config.costs)
    venue = ReplayVenue(folder / "venue.sqlite3", fixture.config)

    def executor(cfg):
        return PaperExecutor(journal, venue, cfg, clock=lambda: clock[0], alert=alerts.append,
                             tag_factory=lambda: f"exit{next(ids):06}")

    funded = executor(fixture.config)
    plan = build_stop_plan(
        raw["side"], Decimal(raw["futures_entry"]), Decimal(raw["atr"]), Decimal(raw["abs_delta"]),
        Decimal(raw["option_entry"]), config.strategy_a,
    )
    result = funded.enter(
        "exit-fixture-position", "vwap", fixture.selection, fixture.entry, spot=fixture.spot,
        premium_stop=plan.premium_stop, target_mid=plan.premium_target,
        target_bid=plan.premium_target - (fixture.entry.asks[0].price - fixture.entry.bids[0].price) / 2,
        decision_detail={"execution_stop_plan": plan.view()},
    )
    if result.status != "submitted" or not funded.positions():
        raise ValueError(f"Could not seed paper position ({result.reason}).")
    exit_config = fixture.config.model_copy(update={"risk": config.risk})
    engine = ExitEngine(executor(exit_config), {fixture.contract.token: fixture.contract}, folder)
    return Drill(fixture, funded, engine, venue, clock, alerts)


def _switch_drill(case: dict, drill: Drill, book: dict) -> dict:
    engine, store = drill.engine, drill.engine.store
    start = drill.clock[0]
    at = start + timedelta(seconds=case.get("seconds", case.get("window_seconds", 1)))
    wait = bool(case.get("wait_for_quote") or case.get("wait_for_confirmation"))
    health = Health(
        last_option_tick_at=start if case.get("wait_for_quote") else at,
        ws_disconnected_at=start if case["reason"] == "websocket_down" else None,
        position_mismatch=case.get("mismatch", False), rejections=case.get("rejections", 0),
        insufficient_funds=case.get("insufficient_funds", False), dashboard_stop=case.get("dashboard_stop", False),
        clock_drift_seconds=Decimal(case["drift_seconds"]) if "drift_seconds" in case else None,
    )
    if case.get("sentinel"):
        sentinel = engine.kill_switch.sentinel
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.touch()
    if "from" in case:
        drill.step(start, quote=drill.book(book), underlying=Decimal(case["from"]))
    if case.get("mismatch"):
        drill.venue.mismatch = True
    drill.clock[0] = at
    quote = None if case.get("wait_for_quote") else drill.book(book)
    flat = drill.step(at, quote=quote, health=health, underlying=Decimal(case["to"]) if "to" in case else None)
    sold_before_confirmation = any(item.intent.side == "SELL" for item in drill.venue.order_book())
    if wait:
        drill.venue.mismatch = False
        drill.clock[0] = at + timedelta(seconds=1)
        flat = drill.step(drill.clock[0], quote=drill.book(book))
    # Start again from the same books to prove the entry block is durable, with fixture funding present.
    drill.funded.reconcile()
    entry_reject = drill.try_entry(drill.book(book))
    events = store.events()
    trigger = next(row for row in events if row["event"] == "kill_switch_fired")
    return {
        "reason": trigger["reason"], "closed": flat, "halt_latched": (
            at.date() in engine.executor.journal.snapshot().halts
        ),
        "entries_blocked": entry_reject == "session_halted", "entry_reject": entry_reject,
        "sold_before_confirmation": sold_before_confirmation if case.get("mismatch") else None,
        "events": events, "alerts": drill.alerts,
    }


def _square_off(data: dict, drill: Drill, book: dict, *, depth_available: bool) -> dict:
    config = drill.engine.config
    if depth_available:
        start = parse_timestamp(data["at"])
        for attempt in range(len(config.exits.reprice_extra_ticks) + 1):
            drill.clock[0] = start + timedelta(seconds=attempt * config.exits.reprice_seconds)
            drill.step(drill.clock[0], quote=drill.book(book, depth=data["shallow_depth"]))
    drill.clock[0] = parse_timestamp(data["backstop_at"])
    quote = drill.book(book, ask=Decimal(book["ask"]) * 2,
                       depth=book["depth"] if depth_available else data["shallow_depth"])
    flat = drill.step(drill.clock[0], quote=quote, option_ltp=Decimal(data["protection_reference"]))
    events = drill.engine.store.events()
    return {
        "flat": flat, "flat_after_backstop": flat, "events": events,
        "attempts": [item for item in events if item["event"] == "exit_attempt"],
        "alerts": [item["event"] for item in drill.alerts],
        "sell_orders": [
            {"tag": item.intent.tag, "state": str(item.state), "filled_quantity": item.filled_quantity}
            for item in drill.engine.executor.journal.snapshot().orders if item.intent.side == "SELL"
        ],
    }


def replay_exits(path: Path, config: Config) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fixture = load_execution_fixture(path.with_name(data["entry_fixture"]), config)
        switches, plan, book, times = data["switches"], data["plan"], data["exit_book"], data["square_off"]
    except (KeyError, TypeError, ArithmeticError):
        raise ValueError("Invalid paper exit fixture.") from None
    with TemporaryDirectory(prefix="orderbot-phase6-") as directory:
        folder = Path(directory)
        drills = [_switch_drill(case, _open_drill(folder / case["reason"], fixture, config, plan), book)
                  for case in switches]
        return {
            "mode": "paper", "source": "fixtures",
            "risk_inputs_in_config": config.risk.capital_inr is not None and config.risk.risk_per_trade_pct is not None,
            "switches": drills,
            "square_off": _square_off(times, _open_drill(folder / "square", fixture, config, plan), book,
                                     depth_available=True),
            "backstop_no_depth": _square_off(times, _open_drill(folder / "no-depth", fixture, config, plan),
                                             book, depth_available=False),
        }
