from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from orderbot.broker.instruments import Instrument
from orderbot.core.config import CostConfig, RiskConfig
from orderbot.core.money import is_finite_decimal, round_inr

from .cost_model import round_trip_cost
from .position_sizer import PositionSize, size_position


class RiskReason(StrEnum):
    APPROVED = "approved"
    UNKNOWN_STATE = "unknown_state"
    SESSION_HALTED = "session_halted"
    DAILY_LOSS = "daily_loss"
    LOSS_STREAK = "loss_streak"
    RISK_UNCONFIGURED = "risk_unconfigured"
    ENTRY_PENDING = "entry_pending"
    POSITION_LIMIT = "position_limit"
    ENTRY_LIMIT = "entry_limit"
    INVALID_CONTRACT = "invalid_contract"
    INVALID_PLAN = "invalid_plan"
    SIZE_ZERO = "size_zero"
    PREMIUM_CAP = "premium_cap"
    FRICTION_LIMIT = "friction_limit"
    EXIT_NOT_REDUCING = "exit_not_reducing"


@dataclass(frozen=True, slots=True)
class RiskState:
    """Current IST session state from the caller, including net mark-to-market P&L."""

    session_day: date
    entries_today: int
    open_positions: int
    premium_deployed: Decimal
    net_day_pnl: Decimal
    consecutive_losses: int
    pending_entry: bool = False
    session_halted: bool = False

    def __post_init__(self):
        if type(self.session_day) is not date:
            raise ValueError("Session state requires an IST calendar date.")
        counts = (self.entries_today, self.open_positions, self.consecutive_losses)
        if any(type(count) is not int or count < 0 for count in counts):
            raise ValueError("Session counters must be nonnegative integers.")
        if not is_finite_decimal(self.premium_deployed) or self.premium_deployed < 0:
            raise ValueError("Deployed premium must be a nonnegative finite Decimal.")
        if not is_finite_decimal(self.net_day_pnl):
            raise ValueError("Session net P&L must be a finite Decimal.")
        if type(self.pending_entry) is not bool or type(self.session_halted) is not bool:
            raise ValueError("Pending-entry and session-halt flags must be known booleans.")


@dataclass(frozen=True, slots=True)
class EntryPlan:
    instrument: Instrument
    entry_mid: Decimal
    entry_ask: Decimal
    premium_stop: Decimal
    target_mid: Decimal
    target_bid: Decimal
    buy_orders: int = 1
    sell_orders: int = 1


@dataclass(frozen=True, slots=True)
class RiskDecision:
    reason: RiskReason
    quantity: int = 0
    halt_session: bool = False
    sizing: PositionSize | None = None
    projected_buy: Decimal | None = None
    projected_sell: Decimal | None = None
    premium_deployed: Decimal | None = None
    all_in_friction: Decimal | None = None
    net_at_target: Decimal | None = None

    @property
    def allowed(self) -> bool:
        return self.reason == RiskReason.APPROVED


@dataclass(frozen=True, slots=True)
class RiskManager:
    config: RiskConfig
    costs: CostConfig

    def session_stop_reason(self, state: RiskState) -> RiskReason | None:
        if state.session_halted:
            return RiskReason.SESSION_HALTED
        capital = self.config.capital_inr
        if capital is not None and state.net_day_pnl <= -(capital * self.config.daily_loss_limit_pct / 100):
            return RiskReason.DAILY_LOSS
        if state.consecutive_losses >= self.config.max_consecutive_losses:
            return RiskReason.LOSS_STREAK
        return None

    def assess_entry(self, plan: EntryPlan, state: RiskState | None) -> RiskDecision:
        if state is None:
            return RiskDecision(RiskReason.UNKNOWN_STATE)
        stop_reason = self.session_stop_reason(state)
        if stop_reason is not None:
            return RiskDecision(stop_reason, halt_session=True)

        capital, per_trade_risk = self.config.capital_inr, self.config.risk_per_trade_pct
        if capital is None or per_trade_risk is None:
            return RiskDecision(RiskReason.RISK_UNCONFIGURED)
        if state.pending_entry:
            return RiskDecision(RiskReason.ENTRY_PENDING)
        if state.open_positions >= self.config.max_open_positions:
            return RiskDecision(RiskReason.POSITION_LIMIT)
        if state.entries_today >= self.config.max_entries_per_day:
            return RiskDecision(RiskReason.ENTRY_LIMIT)

        option = plan.instrument
        if (
            option.exchange != "NFO" or option.right not in {"CE", "PE"}
            or option.expiry is None or option.expiry < state.session_day
            or not is_finite_decimal(option.strike) or option.strike <= 0
            or type(option.lot_size) is not int or option.lot_size <= 0
            or not is_finite_decimal(option.price_tick) or option.price_tick <= 0
        ):
            return RiskDecision(RiskReason.INVALID_CONTRACT)
        prices = (plan.entry_mid, plan.entry_ask, plan.premium_stop, plan.target_mid, plan.target_bid)
        if not all(is_finite_decimal(price) and price > 0 for price in prices):
            return RiskDecision(RiskReason.INVALID_PLAN)

        slippage = self.costs.slippage_ticks * option.price_tick
        projected_buy = plan.entry_ask + slippage
        projected_sell = plan.target_bid - slippage
        if (
            plan.entry_ask < plan.entry_mid or plan.target_bid > plan.target_mid
            or plan.target_mid <= plan.entry_mid or projected_sell <= 0 or plan.premium_stop >= projected_buy
        ):
            return RiskDecision(RiskReason.INVALID_PLAN)

        sizing = size_position(projected_buy, plan.premium_stop, option.lot_size,
                               capital_inr=capital, risk_per_trade_pct=per_trade_risk)
        quantity = sizing.quantity
        if quantity == 0:
            return RiskDecision(RiskReason.SIZE_ZERO, sizing=sizing)
        if any(type(count) is not int or not 1 <= count <= quantity for count in (plan.buy_orders, plan.sell_orders)):
            return RiskDecision(RiskReason.INVALID_PLAN)

        deployed = state.premium_deployed + projected_buy * quantity
        if deployed > capital * self.config.max_premium_deployed_pct / 100:
            return RiskDecision(RiskReason.PREMIUM_CAP, sizing=sizing, premium_deployed=deployed)
        charges = round_trip_cost(
            projected_buy, projected_sell, quantity, self.costs,
            buy_orders=plan.buy_orders, sell_orders=plan.sell_orders,
        )["total"]
        friction = ((projected_buy - plan.entry_mid) + (plan.target_mid - projected_sell)) * quantity + charges
        friction_limit = (plan.target_mid - plan.entry_mid) * quantity * self.config.max_round_trip_friction_pct / 100
        reason = RiskReason.FRICTION_LIMIT if friction > friction_limit else RiskReason.APPROVED
        return RiskDecision(
            reason, quantity=quantity if reason == RiskReason.APPROVED else 0, sizing=sizing,
            projected_buy=projected_buy, projected_sell=projected_sell, premium_deployed=deployed,
            all_in_friction=round_inr(friction),
            net_at_target=round_inr((projected_sell - projected_buy) * quantity - charges),
        )

    @staticmethod
    def assess_exit(
        token: str, quantity: int, *, held_token: str | None, held_quantity: int, pending_exit_quantity: int,
    ) -> RiskDecision:
        """Approve only quantities that reduce the known long position after pending sells."""
        quantities = (quantity, held_quantity, pending_exit_quantity)
        if (
            not token or token != held_token or any(type(value) is not int for value in quantities)
            or pending_exit_quantity < 0 or not 0 < quantity <= held_quantity - pending_exit_quantity
        ):
            return RiskDecision(RiskReason.EXIT_NOT_REDUCING)
        return RiskDecision(RiskReason.APPROVED, quantity=quantity)
