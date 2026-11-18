import json
from decimal import Decimal
from pathlib import Path

import pytest

from orderbot.broker.instruments import parse_master
from orderbot.core.config import load_config
from orderbot.risk.cost_model import exercise_stt, net_pnl, round_trip_cost


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
CONFIG = load_config(ROOT / "config.yaml")
REFERENCE = json.loads((FIXTURES / "option_costs.json").read_text(encoding="utf-8"))
MASTER = parse_master(json.loads((FIXTURES / "instruments.json").read_text(encoding="utf-8")),
                      CONFIG.market.underlying)
LOT_SIZE = next(row.lot_size for row in MASTER.options if row.token == REFERENCE["option_token"])


@pytest.mark.parametrize("case", REFERENCE["cases"], ids=lambda case: f"{case['lots']}-lots")
@pytest.mark.parametrize("basis", ["spec", "calculator"])
def test_round_trip_components_and_net_pnl(case, basis):
    rates = CONFIG.costs
    if basis == "calculator":
        rates = rates.model_copy(update={
            "exchange_including_ipft_rate": Decimal(REFERENCE["calculator_matching_exchange_including_ipft_rate"]),
        })
    buy = Decimal(REFERENCE["buy_price"])
    sell = Decimal(REFERENCE["sell_price"])
    qty = case["lots"] * LOT_SIZE
    expected = {key: Decimal(case[key]) for key in ("brokerage", "stt", "sebi", "stamp_duty", "gst")}
    expected.update({key: Decimal(case[basis][key]) for key in ("exchange_including_ipft", "total")})
    charges = round_trip_cost(buy, sell, qty, rates)
    assert charges == expected
    assert charges["total"] == sum(value for key, value in charges.items() if key != "total")
    assert net_pnl(buy, sell, qty, rates) == Decimal(case[basis]["net_pnl"])


def test_brokerage_is_per_executed_order_and_reflected_in_gst():
    rates = CONFIG.costs
    args = Decimal("120"), Decimal("130"), LOT_SIZE, rates
    standard = round_trip_cost(*args)
    split = round_trip_cost(*args, buy_orders=3, sell_orders=2)
    assert split["brokerage"] == Decimal("100")
    assert split["gst"] - standard["gst"] == Decimal("10.80")
    assert split["total"] - standard["total"] == Decimal("70.80")
    assert net_pnl(*args, buy_orders=3, sell_orders=2) == Decimal("512.26")


def test_exercise_stt_uses_intrinsic_value_only():
    assert exercise_stt(Decimal("100"), LOT_SIZE, CONFIG.costs) == Decimal("9.75")
    assert exercise_stt(Decimal("0"), LOT_SIZE, CONFIG.costs) == Decimal("0.00")


@pytest.mark.parametrize(("buy", "sell", "qty", "orders"), [
    (Decimal("0"), Decimal("120"), 65, 1),
    (Decimal("120"), Decimal("NaN"), 65, 1),
    (Decimal("120"), Decimal("-1"), 65, 1),
    (120.0, Decimal("130"), 65, 1),
    (Decimal("120"), Decimal("130"), True, 1),
    (Decimal("120"), Decimal("130"), 0, 1),
    (Decimal("120"), Decimal("130"), 65, 0),
    (Decimal("120"), Decimal("130"), 65, 66),
])
def test_invalid_execution_inputs_are_rejected(buy, sell, qty, orders):
    with pytest.raises(ValueError):
        round_trip_cost(buy, sell, qty, CONFIG.costs, buy_orders=orders)
