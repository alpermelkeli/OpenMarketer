"""Tests for what a client is told when the database or the code fails."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openmarketer_api.dependencies import Services, analysis_runs
from openmarketer_api.in_process_runs import InProcessAnalysisRuns
from openmarketer_core.db.session import session_factory
from openmarketer_core.repository_analysis import RepositoryAnalysis

# Port 1 is reserved and nothing listens on it.
UNREACHABLE_URL = "postgresql+psycopg://nobody:hunter2@127.0.0.1:1/nothing?connect_timeout=2"
RUN_URL = f"/v1/projects/{uuid.uuid4()}/analyses/{uuid.uuid4()}"


def never_analyses(repository_url: str, clone_into: Path) -> RepositoryAnalysis:
    raise AssertionError("no analysis may start without a database")


@pytest.fixture
def client(app) -> TestClient:
    """A local client that gets the error response instead of the server's exception."""
    return TestClient(
        app,
        base_url="http://localhost:8000",
        client=("127.0.0.1", 50000),
        raise_server_exceptions=False,
    )


def test_unreachable_database_is_reported_without_connection_details(app, client):
    sessions = session_factory(UNREACHABLE_URL)
    app.state.services = Services(
        sessions=sessions, analysis_runs=InProcessAnalysisRuns(sessions, never_analyses)
    )
    response = client.post(RUN_URL.rsplit("/", 1)[0])
    assert (response.status_code, response.json()) == (
        503,
        {"code": "database_unavailable", "message": "the database could not complete the request"},
    )


def test_unexpected_error_is_reported_without_its_detail(app, client, without_database):
    class Broken:
        def get(self, *, workspace_id, project_id, run_id):
            raise RuntimeError("/Users/someone/.env: hunter2")

    app.dependency_overrides[analysis_runs] = Broken
    response = client.get(RUN_URL)
    assert (response.status_code, response.json()) == (
        500,
        {"code": "internal_error", "message": "the request failed unexpectedly"},
    )
