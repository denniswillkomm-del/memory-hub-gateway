from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "gateway.db",
        jwt_secret="test-secret",
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fast_settings(tmp_path: Path) -> Settings:
    """Settings with a 1-second approval timeout — for tests that expect timeout behaviour."""
    return Settings(
        db_path=tmp_path / "gateway_fast.db",
        jwt_secret="test-secret",
        approval_timeout_seconds=1,
        result_ttl_seconds=60,
    )


@pytest.fixture
def fast_client(fast_settings: Settings) -> TestClient:
    app = create_app(fast_settings)
    with TestClient(app) as c:
        yield c
