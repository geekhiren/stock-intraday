from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from importlib.resources import files
from pathlib import Path


SCHEMA_VERSION = 1


@contextmanager
def open_store(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            script = files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")
            connection.executescript(
                f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
            )
        elif version != SCHEMA_VERSION:
            raise sqlite3.DatabaseError(f"Unsupported OrderBOT schema version {version}.")
        with connection:
            yield connection
