import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orderbot.core.ist_time import IST, as_ist, from_storage, now_ist, to_storage


def test_ist_date_boundary_and_utc_storage():
    utc = datetime(2026, 11, 17, 19, tzinfo=UTC)
    local = as_ist(utc)
    assert local.isoformat() == "2026-11-18T00:30:00+05:30"
    assert local.utcoffset() == timedelta(hours=5, minutes=30)
    assert to_storage(local) == "2026-11-17T19:00:00.000000+00:00"
    assert from_storage(to_storage(local)).isoformat() == local.isoformat()
    assert now_ist().tzinfo == IST


@pytest.mark.parametrize("convert", [as_ist, to_storage])
def test_naive_datetime_rejected(convert):
    with pytest.raises(ValueError, match="timezone-aware"):
        convert(datetime.fromisoformat("2026-11-18"))


def test_storage_decoder_rejects_missing_or_non_utc_offsets():
    for value in ("2026-11-18T00:30:00", "2026-11-18T00:30:00+05:30"):
        with pytest.raises(ValueError):
            from_storage(value)


def test_production_datetime_constructors_specify_timezone():
    root = Path(__file__).resolve().parents[1]
    paths = [root / "run.py", *(root / "src").rglob("*.py")]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = {"datetime"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "datetime":
                aliases.update(
                    name.asname or name.name for name in node.names if name.name == "datetime"
                )
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            function = ast.unparse(call.func)
            target, _, method = function.rpartition(".")
            if target in aliases or target.endswith(".datetime"):
                assert method not in {"utcnow", "utcfromtimestamp", "today", "strptime"}, (
                    path, call.lineno, method
                )
                if method in {"now", "fromtimestamp"}:
                    minimum_args = 1 if method == "now" else 2
                    assert len(call.args) >= minimum_args or _timezone_keyword(call, "tz"), (
                        path, call.lineno, method
                    )
                if method == "combine":
                    assert len(call.args) >= 3 or _timezone_keyword(call, "tzinfo"), (
                        path, call.lineno, method
                    )
            if function in aliases or function.endswith(".datetime"):
                assert len(call.args) >= 8 or _timezone_keyword(call, "tzinfo"), (
                    path, call.lineno, function
                )


def _timezone_keyword(call, name):
    return any(
        kw.arg == name and not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
        for kw in call.keywords
    )
