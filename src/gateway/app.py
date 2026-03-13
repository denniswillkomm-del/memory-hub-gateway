from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request

from gateway.config import Settings, get_settings
from gateway.db import get_connection, run_migrations

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        conn = get_connection(settings.db_path)
        run_migrations(conn, MIGRATIONS_DIR)
        app.state.db = conn
        app.state.settings = settings
        yield
        conn.close()

    app = FastAPI(title="Memory Hub Gateway", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/health/companion")
    def health_companion(request: Request) -> dict[str, object]:
        db = request.app.state.db
        s: Settings = request.app.state.settings
        row = db.execute(
            "SELECT last_heartbeat_at FROM companion_heartbeats ORDER BY last_heartbeat_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {"companion_online": False, "last_seen": None}
        last_seen = row["last_heartbeat_at"]
        last_dt = datetime.fromisoformat(last_seen).astimezone(UTC)
        age = (datetime.now(UTC) - last_dt).total_seconds()
        online = age < s.companion_heartbeat_timeout_seconds
        return {"companion_online": online, "last_seen": last_seen}

    return app
