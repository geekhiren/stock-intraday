import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Thread
from unittest.mock import Mock

import requests
import pytest

from orderbot.broker.instruments import parse_master
from orderbot.broker.ws_client import Level
from orderbot.core.config import load_config
from orderbot.core.feed_status import record_feed_status
from orderbot.core.ist_time import parse_timestamp
from orderbot.core.state_store import open_store
from orderbot.execution.exit_engine import ExitEngine
from orderbot.execution.exit_rules import ExitFrame
from orderbot.execution.exit_store import ExitStore
from orderbot.execution.journal import Journal
from orderbot.execution.paper_executor import PaperExecutor
from orderbot.execution.paper_venue import PaperVenue
from orderbot.options.liquidity_gate import OptionQuote
from orderbot.reporting.alerts import AlertOutbox, DeliveryError, TelegramSender, dispatch_once
from orderbot.reporting.daily_report import build_report, ist_bounds, render_markdown, write_due_report
from orderbot.reporting.dashboard import make_server, status_snapshot
from orderbot.risk.kill_switch import Health
from orderbot.reporting.fixtures import seed_surface_fixture


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "phase7_surface.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
EXPECT = DATA["expected"]
CONFIG = load_config(ROOT / "config.yaml")


def seed(tmp_path):
    db = tmp_path / "reporting.sqlite3"
    at = seed_surface_fixture(FIXTURE, CONFIG, db, venue_db=tmp_path / "venue.sqlite3")
    return db, at


def test_daily_report_recomputes_net_and_fees_to_the_paisa(tmp_path):
    db, at = seed(tmp_path)
    report = build_report(db, at.date())
    assert report["reconciliation"]["status"] == "reconciled"
    assert report["net_paise"] == report["reconciliation"]["recomputed_net_paise"] == EXPECT["net_paise"]
    assert report["charges_paise"] == EXPECT["charges_paise"]
    slip = EXPECT["slippage_paise"]
    assert report["slippage"] == {"actual_paise": slip, "model_paise": slip, "delta_paise": 0, "unavailable_orders": 0}
    assert len(report["trades"]) == EXPECT["trade_count"]
    assert [trade["net_paise"] for trade in report["trades"]] == [trade["expected_net_paise"] for trade in DATA["trades"]]
    assert len(report["skips"]) == EXPECT["skipped_count"]
    assert all(item["exit_at"].endswith("+05:30") for item in report["trades"])
    assert "₹167.01" in render_markdown(report)
    assert "IST" in render_markdown(report)


def test_ist_day_bounds_and_one_paisa_tamper(tmp_path):
    db, at = seed(tmp_path)
    assert ist_bounds(date(2026, 11, 30)) == (
        "2026-11-29T18:30:00.000000+00:00", "2026-11-30T18:30:00.000000+00:00",
    )
    with open_store(db) as connection:
        connection.execute("UPDATE trades SET net_pnl_paise=net_pnl_paise+1 WHERE trade_id='fixture-ce'")
    report = build_report(db, at.date())
    assert report["reconciliation"]["status"] == "mismatch"
    assert report["reconciliation"]["delta_paise"] == 1
    assert report["trades"][0]["checks"] == ["net_pnl_mismatch"]
    assert build_report(db, at.date() + timedelta(days=1))["net_paise"] == 0


def test_outbox_records_entry_exit_and_persists_retry(tmp_path):
    db, at = seed(tmp_path)
    box = AlertOutbox(db)
    assert [row["kind"] for row in box.recent(5)] == ["entry", "exit", "entry", "exit", "entry"]
    ExitStore(db).latch(at, "dashboard_stop")
    ExitStore(db).latch(at, "dashboard_stop")
    assert [row["kind"] for row in box.recent(1)] == ["kill_switch"]

    def unavailable(_):
        raise TimeoutError

    assert dispatch_once(box, unavailable, lambda: at, CONFIG.alerts) == "deferred"
    sent = []
    assert dispatch_once(box, sent.append, lambda: at, CONFIG.alerts) == "sent"
    assert all(row["at"].endswith("+05:30") for row in sent)
    first = next(row for row in box.recent(10) if row["correlation_id"] == "fixture-ce" and row["kind"] == "entry")
    assert first["attempts"] == 1 and first["status"] == "queued"
    later = at + timedelta(seconds=CONFIG.alerts.retry_initial_seconds)
    for _ in range(6):
        dispatch_once(box, sent.append, lambda: later, CONFIG.alerts)
    assert len(sent) == 6
    assert all(row["status"] == "sent" for row in box.recent(10))


def test_legacy_missing_slippage_still_reconciles_and_report_runs_at_ist_cutoff(tmp_path):
    db, at = seed(tmp_path)
    with open_store(db) as connection:
        connection.execute("""
            UPDATE order_events SET detail_json=json_remove(detail_json, '$.touch_value', '$.modeled_slippage')
            WHERE filled_quantity > 0 AND order_tag='surface0BUY'
        """)
    report = build_report(db, at.date())
    assert report["slippage"]["unavailable_orders"] == 1
    assert report["reconciliation"]["delta_paise"] == 0
    before = parse_timestamp(f"{at.date()}T15:34:59+05:30")
    assert write_due_report(CONFIG.reporting, db, tmp_path, before) is None
    due = before + timedelta(seconds=1)
    path = write_due_report(CONFIG.reporting, db, tmp_path, due)
    stamp = path.stat().st_mtime_ns
    assert "₹167.01" in path.read_text(encoding="utf-8")
    assert write_due_report(CONFIG.reporting, db, tmp_path, due) == path
    assert path.stat().st_mtime_ns == stamp


def test_reconciliation_failure_queues_once_and_stale_status_is_visible(tmp_path):
    db, at = seed(tmp_path)
    venue = PaperVenue(tmp_path / "venue.sqlite3", CONFIG)
    record_feed_status(db, "connected", at)
    soon = at + timedelta(seconds=CONFIG.reporting.feed_status_max_age_seconds)
    assert status_snapshot(CONFIG, db, soon, fixture=False)["broker"]["state"] == "stale"
    assert status_snapshot(CONFIG, db, soon, fixture=False)["positions"][0]["premium_stale"]
    with open_store(venue.path) as connection:
        connection.execute("DELETE FROM paper_orders WHERE tag='surface2BUY'")
    executor = PaperExecutor(Journal(db, CONFIG.costs), venue, CONFIG, clock=lambda: at)
    for _ in range(2):
        assert executor.reconcile().reason == "missing_remote_order"
    failures = [event for event in AlertOutbox(db).recent(10) if event["kind"] == "reconciliation_failure"]
    assert len(failures) == 1 and failures[0]["fields"]["reason"] == "missing_remote_order"


def test_telegram_transport_encodes_ist_and_rejects_unconfirmed_delivery(monkeypatch):
    config = CONFIG.alerts
    monkeypatch.setenv(config.telegram_token_env, "123:test-token")
    monkeypatch.setenv(config.telegram_chat_id_env, "fixture-chat")
    reply = Mock(status_code=200)
    reply.json.return_value = {"ok": True}
    post = Mock(return_value=reply)
    sender = TelegramSender(config, post=post)
    event = {"kind": "kill_switch", "correlation_id": "fixture", "at": DATA["clock"], "fields": {"reason": "test"}}
    sender(event)
    assert post.call_args.args[0].endswith("/sendMessage")
    payload = post.call_args.kwargs["json"]
    assert payload["chat_id"] == "fixture-chat" and "11:20:00+05:30 IST" in payload["text"]
    post.side_effect = requests.Timeout("secret in exception URL")
    with pytest.raises(DeliveryError, match="could not be confirmed") as failure:
        sender(event)
    assert "secret" not in str(failure.value)


def test_http_dashboard_reads_fixtures_and_stop_is_local_and_durable(tmp_path):
    db, at = seed(tmp_path)
    with make_server(CONFIG, db, clock=lambda: at, fixture=True, port=0) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            page = requests.get(base, timeout=5)
            assert page.status_code == 200
            assert page.text.count('class="btn danger"') == 1
            assert "Stop trading" in page.text
            state = requests.get(base + "/api/status", timeout=5).json()
            assert state["mode"] == "PAPER" and state["broker"]["state"] == "simulated"
            assert state["now"].endswith("+05:30") and state["daily_loss_limit"] is None
            assert state["positions"][0]["premium"] == "124.35"
            assert state["positions"][0]["underlying_stop"] == "22110.00"
            assert len(state["gates"]) == 9
            token = state["stop_token"]
            assert requests.post(base + "/api/stop", headers={"Origin": "https://invalid.example",
                                 "X-OrderBOT-Stop": token}, timeout=5).status_code == 403
            assert requests.post(base + "/api/stop", headers={"Origin": base}, timeout=5).status_code == 403
            assert requests.get(base, headers={"Host": "invalid.example"}, timeout=5).status_code == 403
            headers = {"Origin": base, "X-OrderBOT-Stop": token}
            for _ in range(2):
                assert requests.post(base + "/api/stop", headers=headers, timeout=5).json()["halted"]
            assert ExitStore(db).latch(at, None) == ("dashboard_stop", False)
            assert [event["kind"] for event in AlertOutbox(db).recent(1)] == ["kill_switch"]
            master_path = FIXTURE.with_name(DATA["instrument_master"])
            master = parse_master(json.loads(master_path.read_text(encoding="utf-8")), CONFIG.market.underlying)
            contract = next(item for item in master.options if item.token == DATA["position"]["token"])
            venue = PaperVenue(tmp_path / "venue.sqlite3", CONFIG)
            executor = PaperExecutor(Journal(db, CONFIG.costs), venue, CONFIG, clock=lambda: at)
            engine = ExitEngine(executor, {contract.token: contract}, tmp_path)
            premium = Decimal(DATA["position"]["ltp"])
            quote = OptionQuote(contract.token, at, at, (Level(premium - contract.price_tick, contract.lot_size, 1),),
                                (Level(premium + contract.price_tick, contract.lot_size, 1),), 0, 0)
            assert engine.step(ExitFrame(at, "timer"), {contract.token: quote}, Health(last_option_tick_at=at))
            after = requests.get(base + "/api/status", timeout=5).json()
            assert after["positions"] == [] and after["trades"][-1]["exit_reason"] == "kill_switch"
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_movers_rank_gainers_losers_and_skip_unusable_quotes():
    from orderbot.data.movers import fixture_movers, stock_universe

    movers = fixture_movers(FIXTURE, parse_timestamp(DATA["clock"]))
    assert movers["state"] == "ok" and movers["at"].endswith("+05:30")
    assert (movers["universe"], movers["quoted"], movers["advancers"], movers["decliners"]) == (16, 15, 8, 6)
    assert [row["symbol"] for row in movers["gainers"][:2]] == ["RELIANCE", "TATAMOTORS"]
    assert [row["symbol"] for row in movers["losers"][:2]] == ["BAJFINANCE", "ADANIENT"]
    assert movers["gainers"][0]["setup"] == "holding_highs" and movers["losers"][0]["setup"] == "holding_lows"
    assert movers["losers"][1]["setup"] is None
    rows = [{"exch_seg": "NFO", "instrumenttype": "FUTSTK", "name": "SBIN", "token": "1", "symbol": "SBIN26NOV26FUT"},
            {"exch_seg": "NSE", "instrumenttype": "", "name": "SBIN", "token": "3045", "symbol": "SBIN-EQ"},
            {"exch_seg": "NSE", "instrumenttype": "", "name": "SBIN", "token": "9", "symbol": "SBIN-BL"},
            {"exch_seg": "NSE", "instrumenttype": "", "name": "NOFUT", "token": "7", "symbol": "NOFUT-EQ"}]
    assert stock_universe(rows) == {"3045": "SBIN"}


def test_a_second_dashboard_cannot_share_the_port(tmp_path):
    db, at = seed(tmp_path)
    with make_server(CONFIG, db, clock=lambda: at, fixture=True, port=0) as server:
        with pytest.raises(OSError):
            make_server(CONFIG, db, clock=lambda: at, fixture=True, port=server.server_port)


def test_dashboard_stop_can_be_resumed_but_other_kill_switches_cannot(tmp_path):
    db, at = seed(tmp_path)
    store = ExitStore(db)
    assert store.resume(at) is False
    store.latch(at, "dashboard_stop")
    assert status_snapshot(CONFIG, db, at, fixture=True)["resumable"]
    assert store.resume(at) is True and store.latch(at, None) == (None, False)
    assert status_snapshot(CONFIG, db, at, fixture=True)["halt"] is None
    store.latch(at, "daily_loss_limit")
    assert store.resume(at) is False and not status_snapshot(CONFIG, db, at, fixture=True)["resumable"]
