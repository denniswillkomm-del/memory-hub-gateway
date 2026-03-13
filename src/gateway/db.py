from __future__ import annotations

import sqlite3
from pathlib import Path


def get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def run_migrations(conn: sqlite3.Connection, migrations_dir: Path) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _migrations (filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.commit()

    applied = {row["filename"] for row in conn.execute("SELECT filename FROM _migrations")}
    pending = sorted(p for p in migrations_dir.glob("*.sql") if p.name not in applied)

    for path in pending:
        sql = path.read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO _migrations (filename, applied_at) VALUES (?, datetime('now'))",
            (path.name,),
        )
        conn.commit()
