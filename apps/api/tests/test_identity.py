"""Tests for who a request is taken to be in single-user local mode.

Any workspace route would do; these use the status of an analysis run, served
by a fake.
"""

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from openmarketer_api.analysis_runs import AnalysisRun, AnalysisStatus
from openmarketer_api.dependencies import analysis_runs

RUN_URL = f"/v1/projects/{uuid.uuid4()}/analyses/{uuid.uuid4()}"
NOT_LOCAL = {
    "code": "not_local_request",
    "message": "without a login the API serves only requests made on the machine it runs on",
}


class AnyRun:
    """Answers every status question with a queued run."""

    def get(self, *, workspace_id, project_id, run_id) -> AnalysisRun:
        return AnalysisRun(
            id=run_id,
            workspace_id=workspace_id,
            project_id=project_id,
            status=AnalysisStatus.QUEUED,
            created_at=datetime(2026, 10, 8, tzinfo=UTC),
        )


@pytest.fixture(autouse=True)
def any_run(app, without_database) -> None:
    app.dependency_overrides[analysis_runs] = AnyRun


def test_request_made_on_this_machine_is_accepted(client):
    assert client.get(RUN_URL).status_code == 200


def test_request_from_the_local_dashboard_is_accepted(client):
    response = client.get(RUN_URL, headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 200


def test_request_from_another_machine_is_refused(app):
    remote = TestClient(app, base_url="http://localhost:8000", client=("203.0.113.7", 50000))
    response = remote.get(RUN_URL)
    assert (response.status_code, response.json()) == (403, NOT_LOCAL)


def test_request_from_a_web_page_of_another_site_is_refused(client):
    response = client.get(RUN_URL, headers={"Origin": "https://evil.example"})
    assert (response.status_code, response.json()) == (403, NOT_LOCAL)


def test_request_addressed_to_a_foreign_host_name_is_refused(app):
    rebound = TestClient(app, base_url="http://evil.example:8000", client=("127.0.0.1", 50000))
    assert rebound.get(RUN_URL).status_code == 403
