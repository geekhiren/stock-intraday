from bisect import bisect_left
from dataclasses import dataclass
from datetime import date

from orderbot.broker.instruments import InstrumentMaster
from orderbot.core.config import OptionsConfig


@dataclass(frozen=True, slots=True)
class ExpiryReview:
    expiry: date
    reason: str
    sessions_inclusive: int | None = None


@dataclass(frozen=True, slots=True)
class ExpiryDecision:
    expiry: date | None
    reason: str
    reviews: tuple[ExpiryReview, ...] = ()


def resolve_weekly_expiry(
    master: InstrumentMaster, day: date, sessions: tuple[date, ...], config: OptionsConfig,
) -> ExpiryDecision:
    """Resolve only listed dates using a supplied exchange trading-session calendar."""
    if any(type(session) is not date for session in sessions) or tuple(sorted(set(sessions))) != sessions:
        raise ValueError("Trading sessions must be unique ascending calendar dates.")
    start = bisect_left(sessions, day)
    if start == len(sessions) or sessions[start] != day:
        return ExpiryDecision(None, "session_not_in_calendar")

    monthly = {item.expiry for item in master.futures if item.expiry is not None and item.expiry >= day}
    months = sorted(expiry.year * 12 + expiry.month for expiry in monthly)
    current_month = day.year * 12 + day.month
    if (
        len(months) != config.monthly_futures_count
        or months[0] not in {current_month, current_month + 1}
        or months != list(range(months[0], months[0] + len(months)))
    ):
        return ExpiryDecision(None, "monthly_series_unverified")

    horizon = max(monthly)
    listed = sorted({item.expiry for item in master.options if item.expiry is not None and item.expiry >= day})
    reviews = []
    incomplete = False
    for expiry in listed:
        if expiry in monthly:
            reviews.append(ExpiryReview(expiry, "monthly_series"))
        elif expiry > horizon:
            reviews.append(ExpiryReview(expiry, "beyond_weekly_classification_horizon"))
        elif expiry > sessions[-1]:
            reviews.append(ExpiryReview(expiry, "calendar_coverage_missing"))
            incomplete = True
        else:
            end = bisect_left(sessions, expiry, lo=start)
            if sessions[end] != expiry:
                reviews.append(ExpiryReview(expiry, "expiry_not_in_calendar"))
                return ExpiryDecision(None, "expiry_not_in_calendar", tuple(reviews))
            count = end - start + 1
            if count <= config.min_later_trading_sessions:
                reviews.append(ExpiryReview(expiry, "expiry_too_close", count))
            else:
                reviews.append(ExpiryReview(expiry, "selected", count))
                return ExpiryDecision(expiry, "selected", tuple(reviews))
    return ExpiryDecision(
        None, "calendar_coverage_missing" if incomplete else "no_eligible_weekly", tuple(reviews),
    )
