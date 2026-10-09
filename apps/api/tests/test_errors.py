"""Tests for what a client is told when the database or the code fails."""

import uuid

import pytest
from fastapi.testclient import TestClient

from openmarketer_api.dependencies import Services
from openmarketer_api.identity import current_workspace_id
from openmarketer_core.db.session import session_factory
from openmarketer_core.repository_analysis.workflow_contract import AnalyzeRepositoryInput

# Port 1 is reserved and nothing listens on it.
UNREACHABLE_URL = "postgresql+psycopg://nobody:hunter2@127.0.0.1:1/nothing?connect_timeout=2"
RUN_URL = f"/v1/projects/{uuid.uuid4()}/analyses/{uuid.uuid4()}"


class NoWorkflows:
    async def start(self, run: AnalyzeRepositoryInput) -> None:
        raise AssertionError("no workflow may be started without a database")


@pytest.fixture
def client(app) -> TestClient:
    """A local client that gets the error response instead of the server's exception."""
    return TestClient(
        app,
        base_url="http://localhost:8000",
        client=("127.0.0.1", 50000),
        raise_server_exceptions=False,
    )


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_unreachable_database_is_reported_without_connection_details(app, client, method):
    app.state.services = Services(
        sessions=session_factory(UNREACHABLE_URL), analysis_workflows=NoWorkflows()
    )
    url = RUN_URL if method == "GET" else RUN_URL.rsplit("/", 1)[0]
    response = client.request(method, url)
    assert (response.status_code, response.json()) == (
        503,
        {"code": "database_unavailable", "message": "the database could not complete the request"},
    )


def test_unexpected_error_is_reported_without_its_detail(app, client, without_database):
    def broken() -> uuid.UUID:
        raise RuntimeError("/Users/someone/.env: hunter2")

    app.dependency_overrides[current_workspace_id] = broken
    response = client.get(RUN_URL)
    assert (response.status_code, response.json()) == (
        500,
        {"code": "internal_error", "message": "the request failed unexpectedly"},
    )
