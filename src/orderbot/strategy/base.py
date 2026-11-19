from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from orderbot.core.ist_time import as_ist
from orderbot.options.strike_selector import StrikeDecision

from .stop_plan import Side, StopPlan


StrategyName = Literal["vwap", "ohl"]


@dataclass(frozen=True, slots=True)
class GateResult:
    status: Literal["pass", "fail", "disabled", "not_evaluated"]
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Signal:
    side: Side
    right: Literal["CE", "PE"]
    selection: StrikeDecision
    stop_plan: StopPlan
    target_bid: Decimal


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    id: str
    candle_at: datetime
    at: datetime
    token: str
    gates: dict[str, GateResult]
    reason: str
    signal: Signal | None = None
    strategy: StrategyName = "vwap"

    def __post_init__(self):
        object.__setattr__(self, "candle_at", as_ist(self.candle_at))
        object.__setattr__(self, "at", as_ist(self.at))

    def view(self) -> dict:
        record = {
            "id": self.id, "candle_at": self.candle_at.isoformat(), "decision_at": self.at.isoformat(),
            "future_token": self.token, "reason": self.reason,
            "gates": {name: asdict(result) for name, result in self.gates.items()},
        }
        if self.signal is not None:
            record.update(side=self.signal.side, right=self.signal.right, option_token=self.signal.selection.selected_token,
                          stop_plan=self.signal.stop_plan.view(), target_bid=str(self.signal.target_bid))
        return record
