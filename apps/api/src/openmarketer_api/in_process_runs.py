"""Analysis runs inside the API process: one background thread per run.

This stands in for the Temporal worker of the design, which is not built. It
makes the endpoints work on one machine and gives up what a workflow engine
provides: runs are kept in memory and are lost when the process stops, a run
that was in progress then is neither resumed nor marked failed, nothing is
retried, and a run cannot be cancelled. Runs are executed one after another.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_api.analysis_runs import (
    AnalysisAlreadyRunning,
    AnalysisRun,
    AnalysisRunNotFound,
    AnalysisStatus,
)
from openmarketer_core.analyzer import AnalysisError
from openmarketer_core.db.evidence_store import save_analysis
from openmarketer_core.db.projects import get_project
from openmarketer_core.db.session import DatabaseError, transaction
from openmarketer_core.intake import IntakeError, remote_repository_url
from openmarketer_core.llm import LLMError
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.repository_analysis import RepositoryAnalysis, failure_message

logger = logging.getLogger(__name__)

# Analyse the repository at a URL, cloning it into the given folder.
AnalyseRepository = Callable[[str, Path], RepositoryAnalysis]

_UNFINISHED = (AnalysisStatus.QUEUED, AnalysisStatus.RUNNING)


class _RunFailed(Exception):
    """The run ended without a stored profile; the message is safe to show."""


class InProcessAnalysisRuns:
    def __init__(self, sessions: sessionmaker[Session], analyse: AnalyseRepository) -> None:
        self._sessions = sessions
        self._analyse = analyse
        self._runs: dict[uuid.UUID, AnalysisRun] = {}
        self._runs_lock = threading.Lock()
        self._one_at_a_time = threading.Lock()

    def start(
        self, session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID
    ) -> AnalysisRun:
        project = get_project(session, workspace_id=workspace_id, project_id=project_id)
        # Projects stored from the command line may point at a local folder.
        repository_url = remote_repository_url(project.source_repo_url)
        run = AnalysisRun(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            project_id=project_id,
            status=AnalysisStatus.QUEUED,
            created_at=datetime.now(UTC),
        )
        with self._runs_lock:
            if any(
                r.project_id == project_id and r.status in _UNFINISHED for r in self._runs.values()
            ):
                raise AnalysisAlreadyRunning(f"project {project_id} is already being analysed")
            self._runs[run.id] = run
        # A daemon thread: stopping the server must not wait minutes for a model.
        threading.Thread(
            target=self._execute, args=(run, repository_url), name=f"analysis-{run.id}", daemon=True
        ).start()
        return run

    def get(
        self, *, workspace_id: uuid.UUID, project_id: uuid.UUID, run_id: uuid.UUID
    ) -> AnalysisRun:
        with self._runs_lock:
            run = self._runs.get(run_id)
        if run is None or (run.workspace_id, run.project_id) != (workspace_id, project_id):
            raise AnalysisRunNotFound(f"analysis {run_id} not found")
        return run

    def _execute(self, run: AnalysisRun, repository_url: str) -> None:
        with self._one_at_a_time:
            self._record(replace(run, status=AnalysisStatus.RUNNING))
            try:
                profile_version = self._analyse_and_store(run.workspace_id, repository_url)
            except _RunFailed as e:
                outcome = replace(run, status=AnalysisStatus.FAILED, error=str(e))
            except Exception:  # noqa: BLE001  (thread boundary: the run must not stay "running")
                logger.exception("analysis %s stopped on an unexpected error", run.id)
                outcome = replace(
                    run, status=AnalysisStatus.FAILED, error="the analysis stopped unexpectedly"
                )
            else:
                outcome = replace(
                    run, status=AnalysisStatus.SUCCEEDED, profile_version=profile_version
                )
            self._record(replace(outcome, finished_at=datetime.now(UTC)))

    def _analyse_and_store(self, workspace_id: uuid.UUID, repository_url: str) -> int:
        """Run the analysis and store its draft; return the stored profile version."""
        with tempfile.TemporaryDirectory(prefix="openmarketer-") as workdir:
            try:
                result = self._analyse(repository_url, Path(workdir) / "repo")
            except (IntakeError, AnalysisError, LLMError, ConfigError) as e:
                raise _RunFailed(failure_message(e, Path(workdir))) from e
        try:
            with transaction(self._sessions) as session:
                saved = save_analysis(
                    session,
                    workspace_id=workspace_id,
                    snapshot=result.intake.snapshot,
                    facts=result.extraction.facts,
                    profile=result.analysis.profile,
                )
        except DatabaseError as e:
            logger.error("the profile of a finished analysis was not stored: %s", e)
            raise _RunFailed("the analysis finished, but its profile could not be stored") from e
        return saved.profile_version

    def _record(self, run: AnalysisRun) -> None:
        with self._runs_lock:
            self._runs[run.id] = run
