import json
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from orderbot.broker.instruments import parse_master
from orderbot.core.config import load_config
from orderbot.risk.position_sizer import size_position
from orderbot.risk.risk_manager import EntryPlan, RiskManager, RiskState


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
CONFIG = load_config(ROOT / "config.yaml")
FIXTURE = json.loads((FIXTURES / "position_risk.json").read_text(encoding="utf-8"))
MASTER = parse_master(json.loads((FIXTURES / "instruments.json").read_text(encoding="utf-8")),
                      CONFIG.market.underlying)
OPTION = next(row for row in MASTER.options if row.token == FIXTURE["option_token"])
PLAN = EntryPlan(OPTION, **{key: D(value) for key, value in FIXTURE["entry_plan"].items()})
STATE = RiskState(
    session_day=date(2026, 11, 18), entries_today=0, open_positions=0,
    premium_deployed=D("0"), net_day_pnl=D("0"), consecutive_losses=0,
)
ENTRY_FIXTURE = FIXTURE["entry_risk"]


def manager(*, capital=ENTRY_FIXTURE["capital"], risk=ENTRY_FIXTURE["risk_per_trade_pct"]):
    return RiskManager(
        CONFIG.risk.model_copy(update={"capital_inr": D(capital), "risk_per_trade_pct": D(risk)}),
        CONFIG.costs,
    )


@pytest.mark.parametrize("case", FIXTURE["sizing"]["cases"])
def test_sizing_floors_using_contract_lot_and_exact_budget(case):
    sizing = FIXTURE["sizing"]
    result = size_position(
        D(sizing["entry_price"]), D(sizing["stop_price"]), OPTION.lot_size,
        capital_inr=D(case["capital"]), risk_per_trade_pct=D(sizing["risk_per_trade_pct"]),
    )
    assert result.risk_budget == D(case["budget"])
    assert result.risk_per_lot == D(sizing["risk_per_lot"])
    assert result.lots == case["lots"]
    assert result.quantity == result.lots * OPTION.lot_size
    assert result.reason == (None if result.lots else "size_zero")


@pytest.mark.parametrize(("stop", "lot"), [
    (D("120"), 65), (D("121"), 65), (D("NaN"), 65), (D("0"), 65), (D("100"), 0), (D("100"), True),
])
def test_bad_sizing_inputs_cannot_produce_an_order_size(stop, lot):
    with pytest.raises(ValueError):
        size_position(D("120"), stop, lot, capital_inr=D("100000"), risk_per_trade_pct=D("0.5"))


def test_entry_costs_include_spread_slippage_and_fill_based_charges():
    decision = manager().assess_entry(PLAN, STATE)
    assert decision.allowed
    assert decision.quantity == OPTION.lot_size
    for field in ("projected_buy", "projected_sell", "premium_deployed", "all_in_friction", "net_at_target"):
        assert getattr(decision, field) == D(ENTRY_FIXTURE[field])


def test_unset_inputs_fail_closed_and_zero_lots_are_a_skip():
    result = RiskManager(CONFIG.risk, CONFIG.costs).assess_entry(PLAN, STATE)
    assert result.reason == "risk_unconfigured"
    assert result.quantity == 0
    skipped = manager(capital="100000").assess_entry(PLAN, STATE)
    assert not skipped.allowed
    assert skipped.reason == "size_zero"
    assert skipped.quantity == 0
    assert skipped.sizing.risk_budget == D("500")
    assert skipped.sizing.risk_per_lot == D("1352")


def test_sizing_with_only_capital_set_returns_no_quantity():
    result = size_position(D("120"), D("99.20"), OPTION.lot_size,
                           capital_inr=D("100000"), risk_per_trade_pct=None)
    assert (result.quantity, result.risk_budget, result.reason) == (0, None, "risk_unconfigured")


def test_premium_cap_treats_exact_equality_as_allowed():
    assert manager(capital="31369", risk="5").assess_entry(PLAN, STATE).allowed
    rejected = manager(capital="31368.99", risk="5").assess_entry(PLAN, STATE)
    assert rejected.reason == "premium_cap"
    assert rejected.quantity == 0


@pytest.mark.parametrize(("changes", "reason", "halts"), [
    ({"pending_entry": True}, "entry_pending", False),
    ({"open_positions": 1, "premium_deployed": D("500")}, "position_limit", False),
    ({"entries_today": 5}, "entry_limit", False),
    ({"net_day_pnl": D(ENTRY_FIXTURE["loss_cutoff"])}, "daily_loss", True),
    ({"consecutive_losses": 3}, "loss_streak", True),
    ({"session_halted": True}, "session_halted", True),
])
def test_session_and_entry_limits(changes, reason, halts):
    outcome = manager().assess_entry(PLAN, replace(STATE, **changes))
    assert not outcome.allowed
    assert outcome.quantity == 0
    assert (outcome.reason, outcome.halt_session) == (reason, halts)


def test_entry_is_allowed_one_paisa_before_daily_stop():
    just_above = D(ENTRY_FIXTURE["loss_cutoff"]) + D("0.01")
    assert manager().assess_entry(PLAN, replace(STATE, net_day_pnl=just_above)).allowed


def test_expected_friction_veto_uses_both_sides_and_strictly_exceeds_limit():
    rates = CONFIG.costs.model_copy(update={
        "brokerage_per_order_inr": D("0"), "stt_sell_rate": D("0"),
        "exchange_including_ipft_rate": D("0"), "sebi_rate": D("0"),
        "stamp_buy_rate": D("0"), "gst_rate": D("0"), "slippage_ticks": 0,
    })
    risk = RiskManager(manager().config, rates)
    at_limit = replace(PLAN, target_bid=D("128.10"))
    assert risk.assess_entry(at_limit, STATE).allowed
    outcome = risk.assess_entry(replace(at_limit, target_bid=D("128.05")), STATE)
    assert outcome.reason == "friction_limit"
    assert outcome.quantity == 0
    assert manager().assess_entry(replace(PLAN, target_mid=D("122"), target_bid=D("121.40")), STATE).reason == "friction_limit"


def test_permissions_for_unknown_expired_or_inconsistent_inputs():
    risk = manager()
    assert risk.assess_entry(PLAN, None).reason == "unknown_state"
    expired = replace(STATE, session_day=date(2026, 12, 9))
    assert risk.assess_entry(PLAN, expired).reason == "invalid_contract"
    assert risk.assess_entry(replace(PLAN, instrument=MASTER.futures[0]), STATE).reason == "invalid_contract"
    assert risk.assess_entry(replace(PLAN, target_bid=D("140")), STATE).reason == "invalid_plan"
    assert risk.assess_entry(replace(PLAN, buy_orders=66), STATE).reason == "invalid_plan"


def test_reduce_only_exit_reserves_pending_sells_and_works_without_risk_inputs():
    risk = RiskManager(CONFIG.risk, CONFIG.costs)
    permitted = risk.assess_exit(OPTION.token, 45, held_token=OPTION.token, held_quantity=65,
                                 pending_exit_quantity=20)
    assert permitted.allowed and permitted.quantity == 45
    assert not risk.assess_exit(OPTION.token, 46, held_token=OPTION.token, held_quantity=65,
                               pending_exit_quantity=20).allowed
    assert not risk.assess_exit("other", 65, held_token=OPTION.token, held_quantity=65,
                               pending_exit_quantity=0).allowed
