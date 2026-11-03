"""Test fixtures: the app is built from an explicit Settings object, never env."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import ProviderConfig, Settings
from app.main import create_app

ADMIN_TOKEN = "admin-test-token"


def mock_settings(tmp_path: Path, **overrides) -> Settings:
    base = Settings(
        providers=(ProviderConfig(name="mock", type="mock"),),
        routes={"mock": ("mock:mock-echo",), "default": ("mock:mock-echo",)},
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'aigate-test.db').as_posix()}",
        admin_token=ADMIN_TOKEN,
        usage_flush_interval_s=0.05,
    )
    return replace(base, **overrides)


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    with TestClient(create_app(mock_settings(tmp_path))) as test_client:
        yield test_client


@pytest.fixture()
def auth_client(tmp_path: Path) -> TestClient:
    with TestClient(create_app(mock_settings(tmp_path, api_keys=("sk-test-123",)))) as test_client:
        yield test_client


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"x-admin-token": ADMIN_TOKEN}
