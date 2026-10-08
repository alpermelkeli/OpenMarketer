"""Fixtures for the API tests: an application without services and a local client.

Tests put fakes or the test database in place by overriding dependencies. The
client looks like a request made on the machine the API runs on, which is what
single-user local mode requires.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from openmarketer_api.dependencies import db_session
from openmarketer_api.identity import current_workspace_id
from openmarketer_api.main import create_app


@pytest.fixture
def app() -> FastAPI:
    return create_app()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000))


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.UUID("11111111-1111-4111-8111-111111111111")


@pytest.fixture
def without_database(app, workspace_id) -> None:
    """For routes tested against fakes: a fixed workspace and a session nothing uses."""
    app.dependency_overrides[db_session] = lambda: Session()
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
