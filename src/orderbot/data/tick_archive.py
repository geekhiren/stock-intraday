from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from orderbot.core.atomic_file import atomic_path
from orderbot.core.ist_time import as_ist


SCHEMA = pa.schema([
    pa.field("received_at", pa.timestamp("us", tz="UTC"), nullable=False),
    pa.field("payload", pa.binary()),
    pa.field("discontinuity", pa.bool_(), nullable=False),
])


@dataclass(frozen=True, slots=True)
class ArchiveRecord:
    received_at: datetime
    raw: bytes | None
    discontinuity: bool = False


def _parts(directory: Path, day: date) -> list[Path]:
    return sorted((directory / day.isoformat()).glob("part-*.parquet"))


class TickArchive:
    """Write dated, atomic Parquet partitions for the single paper capture process."""

    def __init__(
        self, directory: Path, day: date, batch_rows: int, flush_seconds: float | None = None,
    ):
        self.directory = directory / day.isoformat()
        self.day = day
        self.batch_rows = batch_rows
        self.flush_interval = timedelta(seconds=flush_seconds) if flush_seconds is not None else None
        parts = _parts(directory, day)
        self._next_part = int(parts[-1].stem.removeprefix("part-")) + 1 if parts else 0
        self._buffer: list[ArchiveRecord] = []
        self._last: datetime | None = None
        if parts:
            stamp = pq.read_table(parts[-1], columns=["received_at"])["received_at"]
            self._last = as_ist(stamp[-1].as_py())

    def __enter__(self) -> TickArchive:
        return self

    @property
    def has_parts(self) -> bool:
        return self._next_part > 0

    def __exit__(self, *_):
        self.flush()

    def append(self, record: ArchiveRecord) -> None:
        timestamp = as_ist(record.received_at)
        if timestamp.date() != self.day:
            raise ValueError("Tick belongs to a different IST session date.")
        if self._last is not None and timestamp < self._last:
            raise ValueError("Tick archive timestamps moved backwards.")
        self._last = timestamp
        self._buffer.append(ArchiveRecord(timestamp, record.raw, record.discontinuity))
        time_to_flush = (
            self.flush_interval is not None
            and timestamp - self._buffer[0].received_at >= self.flush_interval
        )
        if len(self._buffer) >= self.batch_rows or time_to_flush:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        destination = self.directory / f"part-{self._next_part:016d}.parquet"
        rows = [
            {"received_at": event.received_at, "payload": event.raw, "discontinuity": event.discontinuity}
            for event in self._buffer
        ]
        table = pa.Table.from_pylist(rows, schema=SCHEMA)
        with atomic_path(destination) as temporary:
            pq.write_table(table, temporary)
        self._next_part += 1
        self._buffer.clear()


def read_archive(directory: Path, day: date) -> Iterator[ArchiveRecord]:
    paths = _parts(directory, day)
    if not paths:
        raise FileNotFoundError(f"No tick archive for {day}.")
    for path in paths:
        part = pq.ParquetFile(path)
        if not part.schema_arrow.equals(SCHEMA):
            raise ValueError(f"Unexpected tick archive schema in {path.name}.")
        for batch in part.iter_batches():
            for row in batch.to_pylist():
                yield ArchiveRecord(as_ist(row["received_at"]), row["payload"], row["discontinuity"])
