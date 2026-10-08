"""Tests for the analysis routes, against a fake that never runs an analysis."""

import uuid
from datetime import UTC, datetime

import pytest

from openmarketer_api.analysis_runs import (
    AnalysisAlreadyRunning,
    AnalysisRun,
    AnalysisRunNotFound,
    AnalysisStatus,
)
from openmarketer_api.dependencies import analysis_runs
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.intake import IntakeError

PROJECT_ID = uuid.uuid4()
STARTED_AT = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


class FakeRuns:
    """Queues a run on ``start`` unless told to refuse, and hands back what it holds."""

    def __init__(self) -> None:
        self.runs: dict[uuid.UUID, AnalysisRun] = {}
        self.refusal: Exception | None = None

    def start(self, session, *, workspace_id, project_id) -> AnalysisRun:
        if self.refusal is not None:
            raise self.refusal
        run = AnalysisRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            project_id=project_id,
            status=AnalysisStatus.QUEUED,
            created_at=STARTED_AT,
        )
        self.runs[run.id] = run
        return run

    def get(self, *, workspace_id, project_id, run_id) -> AnalysisRun:
        run = self.runs.get(run_id)
        if run is None or (run.workspace_id, run.project_id) != (workspace_id, project_id):
            raise AnalysisRunNotFound(f"analysis {run_id} not found")
        return run


@pytest.fixture
def runs(app, without_database) -> FakeRuns:
    fake = FakeRuns()
    app.dependency_overrides[analysis_runs] = lambda: fake
    return fake


def analyses_url(project_id: uuid.UUID = PROJECT_ID) -> str:
    return f"/v1/projects/{project_id}/analyses"


def test_started_analysis_is_accepted_with_a_run_to_poll(client, runs):
    response = client.post(analyses_url())
    (run,) = runs.runs.values()
    assert response.status_code == 202
    assert response.json() == {
        "id": str(run.id),
        "project_id": str(PROJECT_ID),
        "status": "queued",
        "created_at": "2026-10-08T12:00:00Z",
        "finished_at": None,
        "error": None,
        "profile_version": None,
    }


def test_analysis_is_started_in_the_current_workspace(client, runs, workspace_id):
    client.post(analyses_url())
    (run,) = runs.runs.values()
    assert run.workspace_id == workspace_id


@pytest.mark.parametrize(
    ("refusal", "status", "code"),
    [
        (ProjectNotFound("project not found"), 404, "project_not_found"),
        (AnalysisAlreadyRunning("already being analysed"), 409, "analysis_already_running"),
        (IntakeError("repository URL must start with https://"), 400, "invalid_repository_url"),
    ],
)
def test_refused_start_is_reported_with_its_reason(client, runs, refusal, status, code):
    runs.refusal = refusal
    response = client.post(analyses_url())
    assert (response.status_code, response.json()) == (
        status,
        {"code": code, "message": str(refusal)},
    )


def test_status_shows_how_a_finished_run_ended(client, runs, workspace_id):
    run_id = uuid.UUID(client.post(analyses_url()).json()["id"])
    runs.runs[run_id] = AnalysisRun(
        id=run_id,
        workspace_id=workspace_id,
        project_id=PROJECT_ID,
        status=AnalysisStatus.FAILED,
        created_at=STARTED_AT,
        finished_at=STARTED_AT,
        error="git clone failed: repository not found",
    )
    body = client.get(f"{analyses_url()}/{run_id}").json()
    assert (body["status"], body["error"], body["finished_at"]) == (
        "failed",
        "git clone failed: repository not found",
        "2026-10-08T12:00:00Z",
    )


def test_unknown_run_is_not_found(client, runs):
    response = client.get(f"{analyses_url()}/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["code"] == "analysis_not_found"


def test_run_is_not_found_under_another_project(client, runs):
    run_id = client.post(analyses_url()).json()["id"]
    assert client.get(f"{analyses_url(uuid.uuid4())}/{run_id}").status_code == 404


def test_malformed_run_identifier_is_rejected(client, runs):
    assert client.get(f"{analyses_url()}/not-a-uuid").status_code == 422
