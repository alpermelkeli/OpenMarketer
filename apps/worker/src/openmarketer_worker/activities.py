"""The activities of ``AnalyzeRepository``: each calls core and reports to Temporal.

They hold no rule of their own. What they add is what an activity owes the
engine: every failure is raised with a type that says whether another attempt
is worth its cost and with a message that may be stored and shown, the long
one sends heartbeats, and all three can run twice without doing their work
twice (the run's transition rules in ``db.analysis_runs`` see to that).

Dependencies are handed in when the worker starts; nothing here reads the
environment. The original error of a failure is logged, not attached: Temporal
stores a failure with its causes, and those can name local paths.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from openmarketer_core.analysis_workflow import AnalyzeRepositoryInput
from openmarketer_core.db.analysis_runs import (
    AnalysisRunFinished,
    AnalysisRunNotFound,
    analysis_run,
    mark_run_failed,
    mark_run_started,
    mark_run_succeeded,
)
from openmarketer_core.db.evidence_store import save_analysis_of_project
from openmarketer_core.db.models import AnalysisRunStatus
from openmarketer_core.db.projects import ProjectNotFound, get_project
from openmarketer_core.db.session import DatabaseError, transaction
from openmarketer_core.intake import IntakeError, remote_repository_url
from openmarketer_core.llm import LLMError
from openmarketer_core.repository_analysis import (
    ANALYSIS_FAILURES,
    RepositoryAnalysis,
    failure_message,
)
from openmarketer_worker import steps
from openmarketer_worker.policy import AnalysisPolicy

logger = logging.getLogger(__name__)

# Analyse the repository at a URL, cloning it into the given folder.
AnalyseRepository = Callable[[str, Path], RepositoryAnalysis]


class AnalysisActivities:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        analyse: AnalyseRepository,
        policy: AnalysisPolicy,
    ) -> None:
        self._sessions = sessions
        self._analyse = analyse
        self._policy = policy

    @activity.defn(name=steps.START_RUN)
    def start_run(self, run: AnalyzeRepositoryInput) -> AnalysisPolicy:
        """Mark the run as picked up and say how this worker limits the analysis."""
        with _worded_failures(run):
            try:
                with transaction(self._sessions) as session:
                    mark_run_started(session, **_scope(run))
            except AnalysisRunNotFound as e:
                # The request that created the run may not have committed it yet.
                raise _failure(steps.RUN_NOT_FOUND_YET, str(e)) from None
            except AnalysisRunFinished as e:
                raise _failure(steps.RUN_UNAVAILABLE, str(e)) from None
        return self._policy

    @activity.defn(name=steps.ANALYSE_AND_STORE)
    async def analyse_and_store(self, run: AnalyzeRepositoryInput) -> None:
        """Analyse the project's repository and store the draft as the run's result.

        The work blocks for minutes, so it runs in a thread while this
        coroutine sends heartbeats. A heartbeat says the worker process is
        alive, not that the analysis is making progress; the timeout of the
        attempt bounds that.
        """
        work = asyncio.create_task(asyncio.to_thread(self._analyse_and_store, run))
        # When this coroutine is cancelled nobody else collects the thread's outcome.
        work.add_done_callback(lambda finished: finished.cancelled() or finished.exception())
        try:
            while not work.done():
                activity.heartbeat()
                await asyncio.wait({work}, timeout=self._policy.heartbeat_every_seconds)
        except asyncio.CancelledError:
            # A thread cannot be stopped. Whatever it still does is settled by the run's
            # transition rules: a result for a run already marked failed is not stored.
            logger.warning(
                "analysis run %s was cancelled while its analysis was running", run.run_id
            )
            raise
        work.result()

    @activity.defn(name=steps.RECORD_FAILURE)
    def record_failure(self, failed: steps.RunFailure) -> None:
        """Mark the run failed with the reason, unless it is finished or does not exist."""
        with _worded_failures(failed.run):
            try:
                with transaction(self._sessions) as session:
                    mark_run_failed(session, **_scope(failed.run), error=failed.error)
            except (AnalysisRunNotFound, AnalysisRunFinished) as e:
                # Nothing to record: there is no such run, or its analysis did store a result.
                logger.info("failure of analysis run %s not recorded: %s", failed.run.run_id, e)

    def _analyse_and_store(self, run: AnalyzeRepositoryInput) -> None:
        with _worded_failures(run):
            self._store_analysis_once(run)

    def _store_analysis_once(self, run: AnalyzeRepositoryInput) -> None:
        repository_url = self._repository_to_analyse(run)
        if repository_url is None:
            return
        with tempfile.TemporaryDirectory(prefix="openmarketer-") as workdir:
            try:
                result = self._analyse(repository_url, Path(workdir) / "repo")
            except ANALYSIS_FAILURES as e:
                logger.info("analysis run %s failed: %s", run.run_id, e)
                # Only the model provider fails in ways that pass: an outage, a rate limit.
                kind = steps.MODEL_UNAVAILABLE if isinstance(e, LLMError) else steps.ANALYSIS_FAILED
                raise _failure(kind, failure_message(e, Path(workdir))) from None
        self._store(run, result)

    def _repository_to_analyse(self, run: AnalyzeRepositoryInput) -> str | None:
        """The URL to clone, or ``None`` when an earlier attempt already stored the result."""
        try:
            with transaction(self._sessions) as session:
                stored_run = analysis_run(session, **_scope(run))
                if stored_run.status is AnalysisRunStatus.SUCCEEDED:
                    return None
                if stored_run.status is AnalysisRunStatus.FAILED:
                    raise AnalysisRunFinished(run.run_id, stored_run.status)
                project = get_project(
                    session, workspace_id=run.workspace_id, project_id=run.project_id
                )
            # The worker clones only what a remote caller may name, never a local folder.
            return remote_repository_url(project.source_repo_url)
        except (AnalysisRunNotFound, AnalysisRunFinished, ProjectNotFound) as e:
            raise _failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except IntakeError as e:
            raise _failure(steps.ANALYSIS_FAILED, str(e)) from None

    def _store(self, run: AnalyzeRepositoryInput, result: RepositoryAnalysis) -> None:
        """Store the result and mark the run succeeded, both or neither."""
        try:
            with transaction(self._sessions) as session:
                saved = save_analysis_of_project(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    snapshot=result.intake.snapshot,
                    facts=result.extraction.facts,
                    profile=result.analysis.profile,
                )
                mark_run_succeeded(session, **_scope(run), profile_id=saved.profile_id)
        except AnalysisRunFinished as e:
            # Another attempt finished the run first; what this one stored was rolled back.
            if self._succeeded(run):
                return
            raise _failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except (AnalysisRunNotFound, ProjectNotFound) as e:
            raise _failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except DatabaseError:
            logger.exception("the profile of analysis run %s was not stored", run.run_id)
            raise _failure(steps.DATABASE_UNAVAILABLE, steps.NOT_STORED) from None

    def _succeeded(self, run: AnalyzeRepositoryInput) -> bool:
        with transaction(self._sessions) as session:
            return analysis_run(session, **_scope(run)).status is AnalysisRunStatus.SUCCEEDED


@contextmanager
def _worded_failures(run: AnalyzeRepositoryInput) -> Iterator[None]:
    """Let only failures leave an activity whose type and message were chosen here.

    Temporal stores whatever an activity raises. An error nobody worded is
    logged and replaced, so the history never holds a message written for a log.
    """
    try:
        yield
    except ApplicationError:
        raise
    except DatabaseError:
        logger.exception("analysis run %s: the database could not be used", run.run_id)
        raise _failure(steps.DATABASE_UNAVAILABLE, steps.DATABASE_FAILED) from None
    except Exception:  # noqa: BLE001  (activity boundary, see the docstring)
        logger.exception("analysis run %s stopped on an unexpected error", run.run_id)
        raise _failure(steps.UNEXPECTED, steps.STOPPED_UNEXPECTEDLY) from None


def _scope(run: AnalyzeRepositoryInput) -> dict[str, uuid.UUID]:
    return {
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "run_id": run.run_id,
    }


def _failure(kind: str, message: str) -> ApplicationError:
    return ApplicationError(message, type=kind, non_retryable=kind in steps.NOT_RETRIED)
