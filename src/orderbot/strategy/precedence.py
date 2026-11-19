from datetime import datetime

from orderbot.core.config import Config
from orderbot.core.ist_time import IST
from orderbot.data.candle_builder import MINUTE


def ohl_filled_on(orders, at: datetime) -> bool:
    return any(item.intent.strategy == "ohl" and item.intent.side == "BUY" and item.filled_quantity
               and item.first_fill_at.date() == at.date() for item in orders)


def entry_precedence(config: Config, strategy: str, at: datetime, orders) -> str | None:
    if config.runtime.strategy == "vwap":
        return "ohl_disabled" if strategy == "ohl" else None
    before_cutoff = at.time() < config.strategy_b.entry_window_end
    if strategy == "ohl":
        if not before_cutoff:
            return "outside_ohl_window"
        opened = datetime.combine(at.date(), config.market.session_open, tzinfo=IST)
        if at < opened + config.strategy_b.range_minutes * MINUTE:
            return "outside_ohl_window"
        return "ohl_trade_limit" if ohl_filled_on(orders, at) else None
    if before_cutoff:
        return "vwap_suppressed_ohl_window"
    return "vwap_suppressed_ohl_traded" if ohl_filled_on(orders, at) else None
