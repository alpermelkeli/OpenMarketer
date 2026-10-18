"""The three activities the hooks of ``CreativeStudio`` call: a run starts, failed, ended.

They hold no rule of their own. What they add is what an activity owes the engine: every
failure is raised with a type that says whether another attempt is worth its cost and with a
message that may be stored and shown, and each can run twice without doing its work twice
(the run's transition rules in ``db.studio_runs`` see to that, and ``record_end`` writes its
event in the transaction that reads the run). ``record_failure`` records what the run's paid
operations cost, as their versions were stored. ``record_end`` tells the chat that asked for
the run, if one did, how it ended: the run's last event, and which chat that is, so the
workflow can start the chat's follow-up turn.

None makes or stores an image: the steps of the graph and its actions are the agent runtime's
activities, with what ``studio.py`` hands them, and a run that is over is forgotten by the
runtime's ``forget_run``, whichever way it ended.

Dependencies are handed in when the worker starts; nothing here reads the environment. The
original error of a failure is logged, not attached: Temporal stores a failure with its causes.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from openmarketer_core.db.media_assets import cost_of_run_versions
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.run_events import append_events
from openmarketer_core.db.session import transaction
from openmarketer_core.db.studio_runs import (
    StudioRunFinished,
    StudioRunNotFound,
    mark_run_failed,
    mark_run_started,
    studio_run,
)
from openmarketer_core.run_events import FAILED, FINISHED, RunEvent
from openmarketer_core.studio.workflow_contract import STUDIO_RUN_KIND, StudioRunInput
from openmarketer_worker.agent_runtime.activity_support import worded_failures
from openmarketer_worker.creative_studio import steps
from openmarketer_worker.creative_studio.policy import StudioPolicy

logger = logging.getLogger(__name__)


class StudioActivities:
    def __init__(self, sessions: sessionmaker[Session], policy: StudioPolicy) -> None:
        self._sessions = sessions
        self._policy = policy

    @activity.defn(name=steps.START_RUN)
    def start_run(self, run: StudioRunInput) -> StudioPolicy:
        """Mark the run as picked up and say how this worker limits the steps of its graph."""
        with worded_failures(steps.STUDIO_TEXTS, subject=subject_of(run.run_id)):
            try:
                with transaction(self._sessions) as session:
                    mark_run_started(session, **scope_of(run))
            except StudioRunNotFound as e:
                # The request that created the run may not have committed it yet.
                raise failure(steps.RUN_NOT_FOUND_YET, str(e)) from None
            except StudioRunFinished as e:
                raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        return self._policy

    @activity.defn(name=steps.RECORD_FAILURE)
    def record_failure(self, failed: steps.RunFailure) -> None:
        """Mark the run failed with the reason and what its stored images cost.

        Nothing is recorded for a run that is finished or does not exist.
        """
        with worded_failures(steps.STUDIO_TEXTS, subject=subject_of(failed.run.run_id)):
            try:
                with transaction(self._sessions) as session:
                    spent = cost_of_run_versions(session, **scope_of(failed.run))
                    mark_run_failed(
                        session, **scope_of(failed.run), error=failed.error, cost_usd=spent
                    )
            except (StudioRunNotFound, StudioRunFinished) as e:
                logger.info("failure of studio run %s not recorded: %s", failed.run.run_id, e)

    @activity.defn(name=steps.RECORD_END)
    def record_end(self, end: steps.RunEnd) -> steps.RunEndRecorded:
        """Record the run's last event, ``finished`` or ``failed``; the chat that asked for it.

        In one transaction with reading the run, so an attempt that is tried again after a
        failure records the event once. A run that does not exist for this workspace and
        project records nothing and has no chat to tell.
        """
        with worded_failures(steps.STUDIO_TEXTS, subject=subject_of(end.run.run_id)):
            try:
                with transaction(self._sessions) as session:
                    run = studio_run(session, **scope_of(end.run))
                    append_events(
                        session,
                        workspace_id=end.run.workspace_id,
                        project_id=end.run.project_id,
                        run_kind=STUDIO_RUN_KIND,
                        run_id=end.run.run_id,
                        events=[last_event(end)],
                    )
            except (StudioRunNotFound, ProjectNotFound) as e:
                logger.info("end of studio run %s not recorded: %s", end.run.run_id, e)
                return steps.RunEndRecorded(chat_id=None)
        return steps.RunEndRecorded(chat_id=run.chat_thread_id)


def last_event(end: steps.RunEnd) -> RunEvent:
    """The event a run that ended as ``end`` says ends its progress."""
    if end.reason is not None:
        return RunEvent(FAILED, {"reason": end.reason})
    detail = {
        "asset_id": end.asset_id,
        "version_id": end.version_id,
        "needs_review": end.needs_review,
    }
    return RunEvent(FINISHED, detail)


def scope_of(run: StudioRunInput) -> dict[str, uuid.UUID]:
    """The run as the scoped functions of ``db.studio_runs`` take it."""
    return {
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "run_id": run.run_id,
    }


def subject_of(run_id: uuid.UUID) -> str:
    """How the log names the run's work."""
    return f"studio run {run_id}"


def failure(kind: str, message: str) -> ApplicationError:
    """The failure of ``kind``, which is not tried again when it cannot pass."""
    return ApplicationError(message, type=kind, non_retryable=kind in steps.NOT_RETRIED)
