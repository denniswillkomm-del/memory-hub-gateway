from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(slots=True)
class Settings:
    db_path: Path
    jwt_secret: str
    access_token_ttl_seconds: int = 900          # 15 min
    refresh_token_ttl_days: int = 90
    pairing_timeout_seconds: int = 300           # 5 min
    approval_timeout_seconds: int = 60
    result_ttl_seconds: int = 600                # 10 min
    companion_heartbeat_timeout_seconds: int = 30
    companion_approval_port: int = 47821
    allowlist_path: Path = field(default_factory=lambda: PROJECT_ROOT / "allowlist.yaml")


def get_settings() -> Settings:
    db_path = Path(
        os.getenv("GATEWAY_DB_PATH", PROJECT_ROOT / "data" / "gateway.db")
    ).expanduser()
    jwt_secret = os.getenv("GATEWAY_JWT_SECRET", "change-me-in-production")
    return Settings(
        db_path=db_path,
        jwt_secret=jwt_secret,
        access_token_ttl_seconds=int(os.getenv("GATEWAY_ACCESS_TOKEN_TTL", "900")),
        refresh_token_ttl_days=int(os.getenv("GATEWAY_REFRESH_TOKEN_TTL_DAYS", "90")),
        approval_timeout_seconds=int(os.getenv("GATEWAY_APPROVAL_TIMEOUT", "60")),
        result_ttl_seconds=int(os.getenv("GATEWAY_RESULT_TTL", "600")),
        companion_heartbeat_timeout_seconds=int(os.getenv("GATEWAY_HEARTBEAT_TIMEOUT", "30")),
    )
