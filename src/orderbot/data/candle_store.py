"""Local store of one-minute candles, so backtests read from disk instead of asking the broker every time.

Rows are SmartAPI candles [timestamp, open, high, low, close, volume]. They are kept per stock per calendar
month as gzip JSON, by default under the user's home folder (outside the project, so a large price history is not
synced to cloud storage). Set ORDERBOT_CANDLE_STORE to use another folder.

    python -m orderbot.data.candle_store status
    python -m orderbot.data.candle_store import FOLDER_OR_FILE [...]
"""
from __future__ import annotations

from datetime import date
import gzip
import json
import os
from pathlib import Path
import sys

from orderbot.core.atomic_file import atomic_path


def store_dir() -> Path:
    return Path(os.environ.get("ORDERBOT_CANDLE_STORE") or Path.home() / ".orderbot-research" / "candles")


def _folder(symbol: str, root: Path | None = None) -> Path:
    return (root or store_dir()) / symbol.replace("&", "_and_")


def _read(path: Path) -> list:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def save(symbol: str, rows: list, root: Path | None = None) -> int:
    """Merge candles into the store. A candle already stored for the same minute is replaced. Returns rows written."""
    months: dict[str, dict[str, list]] = {}
    for row in rows:
        months.setdefault(row[0][:7], {})[row[0]] = row
    folder = _folder(symbol, root)
    folder.mkdir(parents=True, exist_ok=True)
    for month, fresh in months.items():
        path = folder / f"{month}.json.gz"
        merged = ({row[0]: row for row in _read(path)} if path.exists() else {}) | fresh
        with atomic_path(path) as temporary:
            with gzip.open(temporary, "wt", encoding="utf-8") as stream:
                json.dump([merged[stamp] for stamp in sorted(merged)], stream, separators=(",", ":"))
    return sum(len(fresh) for fresh in months.values())


def load(symbol: str, start: date, end: date, root: Path | None = None) -> list:
    """Stored candles of `symbol` from `start` to `end` inclusive, oldest first. Empty when nothing is stored."""
    folder, rows = _folder(symbol, root), []
    first, last = start.isoformat(), end.isoformat()
    for path in sorted(folder.glob("*.json.gz")) if folder.is_dir() else []:
        if first[:7] <= path.name[:7] <= last[:7]:
            rows += [row for row in _read(path) if first <= row[0][:10] <= last]
    return rows


def coverage(symbol: str, root: Path | None = None) -> tuple[str, str, int] | None:
    """(first day, last day, candle count) held for `symbol`, or None."""
    folder = _folder(symbol, root)
    paths = sorted(folder.glob("*.json.gz")) if folder.is_dir() else []
    if not paths:
        return None
    first, last = _read(paths[0]), _read(paths[-1])
    middle = sum(len(_read(path)) for path in paths[1:-1])
    return first[0][0][:10], last[-1][0][:10], len(first) + middle + (len(last) if len(paths) > 1 else 0)


def symbols(root: Path | None = None) -> list[str]:
    root = root or store_dir()
    return sorted(path.name.replace("_and_", "&") for path in root.iterdir() if path.is_dir()) if root.is_dir() else []


def import_path(source: Path, root: Path | None = None) -> dict[str, int]:
    """Import downloaded candles: a JSON file of {symbol: rows}, or a folder of <symbol>/<piece>.json.gz files."""
    counts: dict[str, int] = {}
    if source.is_file():
        opener = gzip.open if source.suffix == ".gz" else open
        with opener(source, "rt", encoding="utf-8") as stream:
            for symbol, rows in json.load(stream).items():
                if rows:
                    counts[symbol] = save(symbol, rows, root)
        return counts
    for folder in sorted(path for path in source.iterdir() if path.is_dir()):
        rows = [row for piece in sorted(folder.glob("*.json.gz")) for row in _read(piece)]
        if rows:
            counts[folder.name.replace("_and_", "&")] = save(folder.name.replace("_and_", "&"), rows, root)
    return counts


def main(arguments: list[str]) -> int:
    if arguments[:1] == ["import"] and len(arguments) > 1:
        for source in arguments[1:]:
            counts = import_path(Path(source))
            print(f"{source}: {len(counts)} stocks, {sum(counts.values()):,} candles")
    elif arguments != ["status"]:
        print(__doc__)
        return 2
    held = symbols()
    spans = [coverage(symbol) for symbol in held]
    size = sum(path.stat().st_size for path in store_dir().rglob("*.json.gz")) if held else 0
    print(f"Store {store_dir()}: {len(held)} stocks, {sum(span[2] for span in spans):,} candles, {size / 1e6:.0f} MB")
    for symbol, span in zip(held, spans):
        print(f"  {symbol:12} {span[0]} to {span[1]}  {span[2]:>9,} candles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
