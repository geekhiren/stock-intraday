import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from orderbot.core.config import load_config
from orderbot.core.event_log import open_logs
from orderbot.core.state_store import SCHEMA_VERSION, open_store
from orderbot.execution.journal import Journal, JournalConflict
from orderbot.execution.lifecycle import LifecycleError, OrderIntent, OrderReport, OrderState, validate_transition
from orderbot.execution.paper_executor import PaperExecutor
from orderbot.execution.paper_venue import PaperVenue
from orderbot.execution.replay import load_execution_fixture, replay_execution


ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config.yaml")
FIXTURE = ROOT / "tests" / "fixtures" / "phase4_execution.json"


class FaultyVenue(PaperVenue):
    def __init__(self, *args, fault=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fault = fault
        self.calls = []

    def submit(self, intention, quote, at):
        self.calls.append("submit")
        if self.fault == "before_accept":
            raise TimeoutError
        result = super().submit(intention, quote, at)
        if self.fault == "after_accept":
            raise TimeoutError
        if self.fault == "crash":
            raise InterruptedError("fixture process stopped before receiving the acknowledgement")
        return result

    def order_book(self):
        self.calls.append("order_book")
        return super().order_book()


def setup(tmp_path, fault=None, *, configurable_fixture_funding=True):
    fixture = load_execution_fixture(FIXTURE, CONFIG)
    config = fixture.config if configurable_fixture_funding else CONFIG
    journal = Journal(tmp_path / "journal.sqlite3", config.costs)
    venue = FaultyVenue(tmp_path / "venue.sqlite3", config, fault=fault)
    at = [fixture.at]
    alerts, sleeps = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        at[0] += timedelta(seconds=seconds)

    def new_executor(book=venue):
        return PaperExecutor(journal, book, config, clock=lambda: at[0], sleep=sleep,
                             tag_factory=iter(f"fixture{i:03}" for i in range(10)).__next__,
                             alert=alerts.append)

    return fixture, journal, venue, at, alerts, sleeps, new_executor


def enter(executor, fixture, signal="fixture-1"):
    return executor.enter(
        signal, "vwap", fixture.selection, fixture.entry, spot=fixture.spot,
        premium_stop=fixture.premium_stop, target_mid=fixture.target_mid, target_bid=fixture.target_bid,
    )


def test_timeout_looks_up_tag_once_and_restart_recovers_partial(tmp_path):
    fixture, journal, venue, at, alerts, sleeps, new_executor = setup(tmp_path, "after_accept")
    executor = new_executor()
    outcome = enter(executor, fixture)
    assert venue.calls == ["submit", "order_book"]
    assert sleeps == [CONFIG.execution.timeout_lookup_delay_seconds]
    order = journal.snapshot().orders[0]
    assert outcome.order_tags == (order.intent.tag,)
    assert order.state is OrderState.PARTIAL
    expected = fixture.expected
    assert order.intent.quantity == expected["entry_requested"]
    assert order.filled_quantity == expected["partial_filled"]
    assert order.average_price == D(expected["buy_fill"])

    second_book = PaperVenue(venue.path, fixture.config)
    restarted = new_executor(second_book)
    assert restarted.reconcile().ok
    open_position, = restarted.positions()
    assert open_position.quantity == expected["partial_filled"]
    assert open_position.premium_risk_at_stop == D(expected["premium_risk_at_stop"])
    at[0] = fixture.at + timedelta(seconds=CONFIG.execution.partial_timeout_seconds)
    fresh = replace(fixture.entry, exchange_at=at[0], received_at=at[0])
    restarted.poll({fixture.contract.token: fresh})
    assert [event["state"] for event in journal.history(order.intent.tag)] == expected["lifecycle"]
    assert restarted.positions()[0].quantity == expected["partial_filled"]
    assert not alerts


def test_missing_timeout_tag_halts_new_entries_and_is_durable(tmp_path):
    fixture, journal, venue, _, alerts, sleeps, new_executor = setup(tmp_path, "before_accept")
    first = enter(new_executor(), fixture)
    assert first.reason == "order_timeout_unresolved"
    assert journal.snapshot().orders[0].state is OrderState.SENT
    assert venue.calls == ["submit", "order_book"]
    assert sleeps == [CONFIG.execution.timeout_lookup_delay_seconds]
    assert alerts
    restarted = new_executor()
    assert not restarted.reconcile().ok
    assert enter(restarted, fixture, "fixture-2").reason == "reconciliation_required"
    assert venue.calls.count("submit") == 1


def test_reconciliation_recovers_accepted_fill_after_process_loss(tmp_path):
    fixture, journal, venue, _, _, _, new_executor = setup(tmp_path, "crash")
    try:
        enter(new_executor(), fixture)
    except InterruptedError:
        pass
    else:
        raise AssertionError("fixture must stop before local acknowledgement")
    assert journal.snapshot().orders[0].state is OrderState.SENT
    assert not journal.snapshot().positions
    second_book = PaperVenue(venue.path, fixture.config)
    restarted = new_executor(second_book)
    assert restarted.reconcile().ok
    assert restarted.positions()[0].quantity == fixture.expected["partial_filled"]
    assert [event["state"] for event in journal.history("fixture000")] == ["INTENT", "SENT", "ACKED", "PARTIAL"]


def test_separate_python_process_reconstructs_open_position(tmp_path):
    fixture, journal, venue, _, _, _, new_executor = setup(tmp_path, "crash")
    with pytest.raises(InterruptedError):
        enter(new_executor(), fixture)
    script = """
import json
import sys
from pathlib import Path
from orderbot.core.config import load_config
from orderbot.core.ist_time import parse_timestamp
from orderbot.execution.journal import Journal
from orderbot.execution.paper_executor import PaperExecutor
from orderbot.execution.paper_venue import PaperVenue
config, journal_path, venue_path = map(Path, sys.argv[1:4])
cfg = load_config(config)
execution = PaperExecutor(Journal(journal_path, cfg.costs), PaperVenue(venue_path, cfg),
                          cfg, clock=lambda: parse_timestamp(sys.argv[4]))
result = execution.reconcile()
print(json.dumps({"ok": result.ok, "positions": [item.view() for item in execution.positions()]}))
"""
    response = json.loads(subprocess.check_output(
        [sys.executable, "-c", script, str(ROOT / "config.yaml"), str(journal.path), str(venue.path),
         fixture.at.isoformat()],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    ))
    assert response["ok"]
    assert response["positions"][0]["quantity"] == fixture.expected["partial_filled"]
    assert D(response["positions"][0]["average_price"]) == D(fixture.expected["buy_fill"])
    assert journal.snapshot().orders[0].state is OrderState.PARTIAL


def test_submitted_tag_was_already_committed_in_an_independent_reader(tmp_path, monkeypatch):
    fixture, journal, venue, _, _, _, new_executor = setup(tmp_path)
    submit = venue.submit

    def check_committed(intention, quote, at):
        assert [event["state"] for event in journal.history(intention.tag)] == ["INTENT", "SENT"]
        return submit(intention, quote, at)

    monkeypatch.setattr(venue, "submit", check_committed)
    assert enter(new_executor(), fixture).status == "submitted"


def test_order_audit_logs_correlated_committed_transitions(tmp_path):
    fixture = load_execution_fixture(FIXTURE, CONFIG)
    with open_logs(CONFIG.logging, tmp_path) as logs:
        journal = Journal(tmp_path / "journal.sqlite3", CONFIG.costs, audit=logs.order_audit)
        venue = PaperVenue(tmp_path / "venue.sqlite3", fixture.config)
        executor = PaperExecutor(journal, venue, fixture.config, clock=lambda: fixture.at)
        assert enter(executor, fixture).status == "submitted"
    lines = (tmp_path / CONFIG.logging.order_audit_path).read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert [event["fields"]["state"] for event in events] == ["INTENT", "SENT", "ACKED", "PARTIAL"]
    assert {event["correlation_id"] for event in events} == {"fixture-1"}
    assert all(event["fields"]["event_at_ist"].endswith("+05:30") for event in events)


def test_venue_consumes_displayed_touch_once_across_children(tmp_path):
    fixture = load_execution_fixture(FIXTURE, CONFIG)
    venue = PaperVenue(tmp_path / "venue.sqlite3", fixture.config)
    contract = fixture.contract
    orders = [
        OrderIntent(tag, "one-group", "vwap", contract.token, contract.symbol, "BUY", contract.lot_size,
                    contract.lot_size, contract.price_tick, D("120.10"), fixture.at, fixture.premium_stop, "test")
        for tag in ("child1", "child2")
    ]
    one, two = [venue.submit(intent, fixture.entry, fixture.at) for intent in orders]
    assert one.filled_quantity == contract.lot_size and one.state is OrderState.FILLED
    assert two.filled_quantity == 0 and two.state is OrderState.ACKED
    with pytest.raises(LifecycleError):
        venue.submit(orders[0], fixture.entry, fixture.at)


def test_rejects_regressing_fills_and_fill_above_buy_limit(tmp_path):
    fixture, journal, _, _, _, _, new_executor = setup(tmp_path)
    enter(new_executor(), fixture)
    previous = journal.snapshot().orders[0]
    with pytest.raises(LifecycleError, match="regressed"):
        validate_transition(previous, OrderReport(previous.intent, OrderState.CANCELLED))
    average_above_limit = OrderReport(
        previous.intent, OrderState.FILLED, 130, previous.filled_value + 65 * D("120.15"),
        previous.first_fill_at, previous.last_fill_at,
    )
    with pytest.raises(LifecycleError, match="order limit"):
        validate_transition(previous, average_above_limit)


def test_prepared_but_never_claimed_order_is_cancelled_on_recovery(tmp_path):
    fixture = load_execution_fixture(FIXTURE, CONFIG)
    journal = Journal(tmp_path / "journal.sqlite3", fixture.config.costs)
    venue = PaperVenue(tmp_path / "venue.sqlite3", fixture.config)
    contract = fixture.contract
    intent = OrderIntent("unsent01", "signal-0", "vwap", contract.token, contract.symbol, "BUY",
                         contract.lot_size, contract.lot_size, contract.price_tick, D("120.10"),
                         fixture.at, fixture.premium_stop, "entry")
    revision = journal.snapshot().revision
    journal.prepare((intent,), revision)
    with pytest.raises(JournalConflict, match="Ledger changed"):
        journal.prepare((replace(intent, tag="unsent02", decision_id="signal-2"),), revision)
    executor = PaperExecutor(journal, venue, fixture.config, clock=lambda: fixture.at)
    assert executor.reconcile().ok
    report, = journal.snapshot().orders
    assert report.state is OrderState.CANCELLED and report.reason == "unsent_intent"
    assert venue.order_book() == ()
    assert executor.reconcile().ok


def test_rejected_order_latches_halt_and_records_alert(tmp_path):
    fixture = load_execution_fixture(FIXTURE, CONFIG)
    cfg = fixture.config.model_copy(update={"execution": fixture.config.execution.model_copy(
        update={"paper_rejection_pct": D("100")},
    )})
    journal = Journal(tmp_path / "journal.sqlite3", cfg.costs)
    venue = PaperVenue(tmp_path / "venue.sqlite3", cfg)
    alerts = []
    executor = PaperExecutor(journal, venue, cfg, clock=lambda: fixture.at, alert=alerts.append)
    assert enter(executor, fixture).reason == "order_rejected"
    assert journal.snapshot().halts[fixture.at.date()] == "order_rejected"
    assert journal.snapshot().orders[0].state is OrderState.REJECTED
    assert not executor.positions()
    assert enter(executor, fixture, "fixture-2").reason == "session_halted"
    assert len(venue.order_book()) == 1 and alerts[0]["reason"] == "order_rejected"


def test_mismatching_remote_position_is_neither_imported_nor_corrected(tmp_path, monkeypatch):
    fixture, journal, venue, at, alerts, _, new_executor = setup(tmp_path)
    executor = new_executor()
    enter(executor, fixture)
    before = journal.snapshot().positions
    read = venue.snapshot
    monkeypatch.setattr(venue, "snapshot", lambda: replace(read(), positions={}))
    at[0] += timedelta(seconds=CONFIG.execution.reconcile_interval_seconds)
    assert not executor.poll({})
    assert journal.snapshot().halts[fixture.at.date()] == "position_mismatch"
    assert journal.snapshot().positions == before
    assert alerts[-1]["event"] == "reconciliation_failed"


def test_checked_in_unfunded_configuration_cannot_reach_venue_submission(tmp_path):
    fixture, journal, venue, _, _, _, new_executor = setup(tmp_path, configurable_fixture_funding=False)
    result = enter(new_executor(), fixture)
    assert result.reason == "risk_unconfigured"
    assert "submit" not in venue.calls
    assert journal.snapshot().orders == ()


def test_fixture_replay_journals_one_closed_trade_with_net_costs():
    first = replay_execution(FIXTURE, CONFIG)
    assert first == replay_execution(FIXTURE, CONFIG)
    assert first["timeout"]["calls"] == ["submit", "order_book"]
    assert first["positions_after_restart"][0]["quantity"] == 65
    assert first["positions_after_exit"] == []
    trade, = first["trades"]
    cash = load_execution_fixture(FIXTURE, CONFIG).expected
    assert D(trade["buy_average"]) == D(cash["buy_fill"])
    assert D(trade["sell_average"]) == D(cash["sell_fill"])
    assert trade["charges"]["total"] == cash["cost"]
    assert trade["net_pnl_inr"] == cash["net_pnl_inr"]


def test_freeze_children_aggregate_to_one_position_and_one_net_trade(tmp_path):
    spec = json.loads(FIXTURE.read_text(encoding="utf-8"))["split"]
    fixture = load_execution_fixture(FIXTURE, CONFIG)
    cfg = fixture.config.model_copy(update={"risk": fixture.config.risk.model_copy(
        update={"capital_inr": D(spec["capital_inr"])},
    )})
    journal = Journal(tmp_path / "split-journal.sqlite3", cfg.costs)
    venue = PaperVenue(tmp_path / "split-venue.sqlite3", cfg)
    at = [fixture.at]
    tags = iter(("entry1", "entry2", "exit1", "exit2"))
    executor = PaperExecutor(journal, venue, cfg, clock=lambda: at[0], tag_factory=tags.__next__)
    depth = spec["touch_depth"]
    entry = replace(fixture.entry, bids=(replace(fixture.entry.bids[0], quantity=depth),),
                    asks=(replace(fixture.entry.asks[0], quantity=depth),))
    assert enter(executor, replace(fixture, entry=entry)).order_tags == ("entry1", "entry2")
    ledger = journal.snapshot()
    assert [order.intent.quantity for order in ledger.orders] == spec["children"]
    position, = ledger.positions
    assert position.quantity == sum(spec["children"])
    at[0] += timedelta(seconds=1)
    exit_quote = replace(entry, exchange_at=at[0], received_at=at[0],
                         bids=(replace(entry.bids[0], price=D("129.95")),),
                         asks=(replace(entry.asks[0], price=D("130.05")),))
    result = executor.exit_position(position.decision_id, fixture.contract, exit_quote, reason="fixture_exit")
    assert result.order_tags == ("exit1", "exit2")
    ledger = journal.snapshot()
    assert not ledger.positions
    trade, = ledger.trades
    assert trade["buy_orders"] == trade["sell_orders"] == 2
    assert trade["charges"]["total"] == spec["charges"]
    assert trade["net_pnl_inr"] == spec["net_pnl_inr"]


def test_version_one_database_migrates_with_prior_decisions_intact(tmp_path):
    db = tmp_path / "previous.sqlite3"
    schema = ROOT / "src" / "orderbot" / "core" / "schema.sql"
    with sqlite3.connect(db) as old:
        old.executescript(schema.read_text(encoding="utf-8"))
        old.execute("PRAGMA user_version=1")
        old.execute("INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?)",
                    ("old-signal", "2026-11-30T03:45:00.000000+00:00", "vwap", "skipped", "fixture", "{}"))
    old.close()
    with open_store(db) as upgraded:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert upgraded.execute("SELECT decision_id FROM decisions").fetchone()[0] == "old-signal"
        assert upgraded.execute("SELECT count(*) FROM execution_halts").fetchone()[0] == 0
