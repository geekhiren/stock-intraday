from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from orderbot.core.config import Config
from orderbot.core.ist_time import as_ist
from orderbot.options.strike_selector import StrikeDecision

from .stop_plan import Side, StopPlan


StrategyName = Literal["vwap", "ohl"]
DECISION_CANDLE_SOURCES = frozenset({"feed", "broker"})


def candle_age_limit(config: Config, source: str) -> float:
    if source == "broker":
        return config.paper_trial.broker_candle_max_age_seconds
    return config.data.websocket_stale_seconds


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
    candle_source: str = "feed"

    def __post_init__(self):
        object.__setattr__(self, "candle_at", as_ist(self.candle_at))
        object.__setattr__(self, "at", as_ist(self.at))

    def view(self) -> dict:
        record = {
            "id": self.id, "candle_at": self.candle_at.isoformat(), "decision_at": self.at.isoformat(),
            "future_token": self.token, "reason": self.reason,
            "gates": {name: asdict(result) for name, result in self.gates.items()},
        }
        if self.candle_source != "feed":
            record["candle_source"] = self.candle_source
        if self.signal is not None:
            record.update(side=self.signal.side, right=self.signal.right, option_token=self.signal.selection.selected_token,
                          stop_plan=self.signal.stop_plan.view(), target_bid=str(self.signal.target_bid))
        return record
