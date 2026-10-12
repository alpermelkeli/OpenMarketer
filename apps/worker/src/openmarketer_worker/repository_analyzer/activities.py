"""The two activities the hooks of ``AnalyzeRepository`` call: a run starts, a run failed.

They hold no rule of their own. What they add is what an activity owes the
engine: every failure is raised with a type that says whether another attempt
is worth its cost and with a message that may be stored and shown, and both
can run twice without doing their work twice (the run's transition rules in
``db.analysis_runs`` see to that).

Neither analyses anything or touches a run's checkpoints or its clone: the
steps of the graph and the clone are the agent runtime's activities, with what
``analysis.py`` hands them, and a run that is over is forgotten by the
runtime's ``forget_run``, whichever way it ended.

Dependencies are handed in when the worker starts; nothing here reads the
environment. The original error of a failure is logged, not attached: Temporal
stores a failure with its causes, and those can name local paths. It also
stores the failure's stack trace, which names source files and lines only.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from openmarketer_core.db.analysis_runs import (
    AnalysisRunFinished,
    AnalysisRunNotFound,
    mark_run_failed,
    mark_run_started,
)
from openmarketer_core.db.session import transaction
from openmarketer_core.repository_analysis.workflow_contract import AnalyzeRepositoryInput
from openmarketer_worker.agent_runtime.activity_support import worded_failures
from openmarketer_worker.repository_analyzer import steps
from openmarketer_worker.repository_analyzer.policy import AnalysisPolicy

logger = logging.getLogger(__name__)


class AnalysisActivities:
    def __init__(self, sessions: sessionmaker[Session], policy: AnalysisPolicy) -> None:
        self._sessions = sessions
        self._policy = policy

    @activity.defn(name=steps.START_RUN)
    def start_run(self, run: AnalyzeRepositoryInput) -> AnalysisPolicy:
        """Mark the run as picked up and say how this worker limits the steps of its graph."""
        with worded_failures(steps.ANALYSIS_TEXTS, subject=subject_of(run.run_id)):
            try:
                with transaction(self._sessions) as session:
                    mark_run_started(session, **scope_of(run))
            except AnalysisRunNotFound as e:
                # The request that created the run may not have committed it yet.
                raise failure(steps.RUN_NOT_FOUND_YET, str(e)) from None
            except AnalysisRunFinished as e:
                raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        return self._policy

    @activity.defn(name=steps.RECORD_FAILURE)
    def record_failure(self, failed: steps.RunFailure) -> None:
        """Mark the run failed with the reason, unless it is finished or does not exist."""
        with worded_failures(steps.ANALYSIS_TEXTS, subject=subject_of(failed.run.run_id)):
            try:
                with transaction(self._sessions) as session:
                    mark_run_failed(session, **scope_of(failed.run), error=failed.error)
            except (AnalysisRunNotFound, AnalysisRunFinished) as e:
                # Nothing to record: no such run for this workspace and project, or its
                # analysis did store a result, or it failed before.
                logger.info("failure of analysis run %s not recorded: %s", failed.run.run_id, e)


def scope_of(run: AnalyzeRepositoryInput) -> dict[str, uuid.UUID]:
    """The run as the scoped functions of ``db.analysis_runs`` take it."""
    return {
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "run_id": run.run_id,
    }


def subject_of(run_id: uuid.UUID) -> str:
    """How the log names the run's work."""
    return f"analysis run {run_id}"


def failure(kind: str, message: str) -> ApplicationError:
    """The failure of ``kind``, which is not tried again when the analysis cannot go on."""
    return ApplicationError(message, type=kind, non_retryable=kind in steps.NOT_RETRIED)
