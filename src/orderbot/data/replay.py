from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from orderbot.broker.ws_client import decode_snap
from orderbot.core.config import DataConfig
from orderbot.core.ist_time import as_ist

from .candle_builder import MINUTE, Candle, CandleBuilder, TradingSession
from .indicators import BarIndicators, IndicatorEngine, PivotLevels, floor_pivots
from .tick_archive import ArchiveRecord


@dataclass(frozen=True, slots=True)
class AnalyzedBar:
    bar: Candle
    indicators: BarIndicators
    source: str = "feed"


class CandlePipeline:
    def __init__(
        self, session: TradingSession, signal_token: str, config: DataConfig, *,
        started_at: datetime | None = None,
        previous: tuple[Candle, ...] = (),
        backfill: tuple[Candle, ...] = (),
    ):
        self.session = session
        self.signal_token = signal_token
        self.config = config
        self.builder = CandleBuilder(session, config.close_grace_seconds, signal_token, started_at=started_at)
        self.indicators = IndicatorEngine(config.rsi_period, config.atr_period)
        self.last_at: datetime | None = None
        self.pivots: PivotLevels | None = None
        self.discontinuities = 0
        self._prior_late_dropped = 0
        start = as_ist(started_at) if started_at is not None else session.opens_at
        if any(candle.at >= session.opens_at for candle in previous):
            raise ValueError("Previous-session indicators need candles before the current session.")
        if any(
            not session.opens_at <= candle.at < session.closes_at or candle.at + MINUTE > start
            for candle in backfill
        ):
            raise ValueError("Backfill may contain only current-session candles closed before capture.")
        if previous:
            for candle in previous:
                self.indicators.update(candle)
            self.pivots = floor_pivots(
                max(candle.high for candle in previous),
                min(candle.low for candle in previous),
                previous[-1].close,
            )
        self.seed = tuple(AnalyzedBar(candle, self.indicators.update(candle), "history") for candle in backfill)

    @property
    def late_dropped(self) -> int:
        return self._prior_late_dropped + self.builder.late_dropped

    def step(self, record: ArchiveRecord) -> tuple[AnalyzedBar, ...]:
        now = as_ist(record.received_at)
        if now.date() != self.session.day:
            raise ValueError("Replay record belongs to a different session.")
        if self.last_at is not None and now < self.last_at:
            raise ValueError("Replay timestamps moved backwards.")
        self.last_at = now
        if record.discontinuity:
            if record.raw is not None:
                raise ValueError("A feed discontinuity cannot contain a market packet.")
            self.discontinuities += 1
            self._prior_late_dropped += self.builder.late_dropped
            self.builder = CandleBuilder(self.session, self.config.close_grace_seconds,
                                         self.signal_token, started_at=now)
            return ()
        trade = decode_snap(record.raw) if record.raw is not None else None
        return tuple(
            AnalyzedBar(candle, self.indicators.update(candle))
            for candle in self.builder.step(now, trade)
        )


def _encode(value):
    if isinstance(value, datetime):
        return as_ist(value).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported replay value of type {type(value).__name__}.")


@dataclass(frozen=True, slots=True)
class ReplayReport:
    session: str
    signal_token: str
    packets: int
    first_received_at: datetime | None
    through: datetime | None
    complete_capture: bool
    late_dropped: int
    discontinuities: int
    rows: tuple[AnalyzedBar, ...]

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=_encode, sort_keys=True, separators=(",", ":"))


def replay_archive(
    records: Iterable[ArchiveRecord], session: TradingSession, signal_token: str, config: DataConfig, *,
    previous: tuple[Candle, ...] = (), backfill: tuple[Candle, ...] = (),
) -> ReplayReport:
    pipeline = None
    first = None
    rows = []
    packets = 0
    for record in records:
        if pipeline is None:
            first = as_ist(record.received_at)
            pipeline = CandlePipeline(
                session, signal_token, config, started_at=first, previous=previous, backfill=backfill,
            )
            rows.extend(pipeline.seed)
        packets += record.raw is not None
        rows.extend(pipeline.step(record))
    if pipeline is None:
        raise ValueError("Tick archive is empty.")
    through = pipeline.last_at
    covers_full_session = (
        first <= session.opens_at
        and through >= session.closes_at + timedelta(seconds=config.close_grace_seconds)
        and pipeline.discontinuities == 0
        and pipeline.late_dropped == 0
        and pipeline.builder.opening_volume_observed
        and any(row.source == "feed" for row in rows)
    )
    return ReplayReport(
        session.day.isoformat(), signal_token, packets, first, through, covers_full_session,
        pipeline.late_dropped, pipeline.discontinuities, tuple(rows),
    )


def compare_candles(
    capture: Iterable[AnalyzedBar], reference: Iterable[Candle], price_tick: Decimal,
) -> tuple[dict, ...]:
    observed = {row.bar.at: row.bar for row in capture if row.source == "feed"}
    expected = {candle.at: candle for candle in reference}
    mismatches = []
    for at in sorted(observed.keys() | expected.keys()):
        differences = {}
        if at not in observed or at not in expected:
            differences["coverage"] = "missing_capture" if at not in observed else "missing_reference"
        else:
            actual, target = observed[at], expected[at]
            for field in ("open", "high", "low", "close", "volume"):
                value, reference_value = getattr(actual, field), getattr(target, field)
                tolerance = 0 if field == "volume" else price_tick
                if abs(value - reference_value) > tolerance:
                    differences[field] = {"capture": str(value), "reference": str(reference_value)}
        if differences:
            mismatches.append({"at": as_ist(at).isoformat(), **differences})
    return tuple(mismatches)
