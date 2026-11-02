"""The three activities the hooks of ``WritePost`` call: a run starts, its post settles, it failed.

They hold no rule of their own: how a run and its content item change state is core's
(``db/writing_runs.py``, ``db/content_items.py``). What they add is what an activity owes the
engine: every failure is raised with a type that says whether another attempt is worth its cost
and with a message that may be stored and shown, and each can run twice without doing its work
twice, because a transition to where a run or an item already is changes nothing.
``record_outcome`` settles the post and marks the run succeeded in one transaction, both or
neither; it never records a post as sent: ``dry_run`` is reached only through the handler of
``publish_post``, once a person approved the post, and ``record_outcome`` accepts it only for an
item that handler recorded so, naming an approved proposal (core refuses the rest);
``record_failure`` records the run failed with what its stored draft cost, which settles a post
nobody decided on ``failed`` with it.

None writes or stores a post: the steps of the graph and its two actions are the agent
runtime's activities, with what ``writing.py`` hands them, and a run that is over is forgotten
by the runtime's ``forget_run``, whichever way it ended.

Dependencies are handed in when the worker starts; nothing here reads the environment. The
original error of a failure is logged, not attached: Temporal stores a failure with its causes.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from openmarketer_core.db.content_items import (
    ContentItemNotFound,
    ContentItemSettled,
    ProposalNotApproved,
    content_item,
    settle_content_item,
)
from openmarketer_core.db.models import ContentItemStatus, WritingRunStatus
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.session import transaction
from openmarketer_core.db.writing_runs import (
    WritingRunFinished,
    WritingRunNotFound,
    WritingRunNotStarted,
    WritingRunUnsettled,
    mark_run_failed,
    mark_run_started,
    mark_run_succeeded,
    writing_run,
)
from openmarketer_core.writing.workflow_contract import WritingRunInput
from openmarketer_worker.agent_runtime.activity_support import worded_failures
from openmarketer_worker.writer import steps
from openmarketer_worker.writer.policy import WriterPolicy

logger = logging.getLogger(__name__)


class WriterActivities:
    def __init__(self, sessions: sessionmaker[Session], policy: WriterPolicy) -> None:
        self._sessions = sessions
        self._policy = policy

    @activity.defn(name=steps.START_RUN)
    def start_run(self, run: WritingRunInput) -> WriterPolicy:
        """Mark the run as picked up and say how this worker limits the steps of its graph.

        The profile version the run writes from is recorded later, when its ``prepare`` reads
        it (``writing.py``): it is not known yet.
        """
        with worded_failures(steps.WRITING_TEXTS, subject=subject_of(run.run_id)):
            try:
                with transaction(self._sessions) as session:
                    mark_run_started(session, **scope_of(run))
            except WritingRunNotFound as e:
                # The request that created the run may not have committed it yet.
                raise failure(steps.RUN_NOT_FOUND_YET, str(e)) from None
            except WritingRunFinished as e:
                raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        return self._policy

    @activity.defn(name=steps.RECORD_OUTCOME)
    def record_outcome(self, outcome: steps.RunOutcome) -> None:
        """Settle the run's post as it ended and mark the run succeeded, with what it cost.

        The item must be the run's own. A run that succeeded before records nothing again. A
        post that ended ``dry_run`` must have been recorded so by ``publish_post``'s handler,
        with the proposal a person approved: an outcome cannot record a post as sent.
        """
        run = outcome.run
        with worded_failures(steps.WRITING_TEXTS, subject=subject_of(run.run_id)):
            status = _settled_status(outcome.status)
            cost = _dollars(outcome.cost_usd)
            try:
                with transaction(self._sessions) as session:
                    stored = writing_run(session, **scope_of(run))
                    if stored.status is WritingRunStatus.SUCCEEDED:
                        logger.info("outcome of writing run %s was recorded before", run.run_id)
                        return
                    if stored.content_item_id != outcome.content_item_id:
                        raise failure(
                            steps.RUN_UNAVAILABLE,
                            f"content item {outcome.content_item_id} is not the post of "
                            f"writing run {run.run_id}",
                        )
                    if status is ContentItemStatus.DRY_RUN:
                        _require_recorded_as_sent(session, run, outcome.content_item_id)
                    settle_content_item(
                        session,
                        workspace_id=run.workspace_id,
                        project_id=run.project_id,
                        item_id=outcome.content_item_id,
                        status=status,
                    )
                    mark_run_succeeded(session, **scope_of(run), cost_usd=cost)
            except ProposalNotApproved as e:
                raise failure(steps.WRITING_FAILED, str(e)) from None
            except (
                WritingRunNotFound,
                WritingRunFinished,
                WritingRunNotStarted,
                WritingRunUnsettled,
                ContentItemNotFound,
                ContentItemSettled,
                ProjectNotFound,
            ) as e:
                raise failure(steps.RUN_UNAVAILABLE, str(e)) from None

    @activity.defn(name=steps.RECORD_FAILURE)
    def record_failure(self, failed: steps.RunFailure) -> None:
        """Mark the run failed with the reason and what its stored draft cost.

        A post the run stored that nobody settled is settled ``failed`` with it. Nothing is
        recorded for a run that is finished or does not exist. What the run spent before it
        stored a draft is not known here (it is in the graph's state only), so a run that
        failed before then is recorded as having cost nothing.
        """
        run = failed.run
        with worded_failures(steps.WRITING_TEXTS, subject=subject_of(run.run_id)):
            try:
                with transaction(self._sessions) as session:
                    stored = writing_run(session, **scope_of(run))
                    spent = Decimal(0)
                    if stored.content_item_id is not None:
                        spent = content_item(
                            session,
                            workspace_id=run.workspace_id,
                            project_id=run.project_id,
                            item_id=stored.content_item_id,
                        ).cost_usd
                    mark_run_failed(session, **scope_of(run), error=failed.error, cost_usd=spent)
            except (WritingRunNotFound, WritingRunFinished, ContentItemNotFound) as e:
                # Nothing to record: no such run for this workspace and project, or its post
                # was settled and the run succeeded, or it failed before.
                logger.info("failure of writing run %s not recorded: %s", run.run_id, e)


def _require_recorded_as_sent(session: Session, run: WritingRunInput, item_id: uuid.UUID) -> None:
    """Refuse for good a ``dry_run`` outcome for an item ``publish_post`` did not record so."""
    item = content_item(
        session, workspace_id=run.workspace_id, project_id=run.project_id, item_id=item_id
    )
    if item.status is not ContentItemStatus.DRY_RUN or item.proposal_id is None:
        raise failure(
            steps.WRITING_FAILED,
            f"content item {item_id} was not recorded as a dry run of an approved post",
        )


def _settled_status(value: str) -> ContentItemStatus:
    """The status a run that succeeded settles its post as; anything else is refused."""
    succeeding = (ContentItemStatus.DRY_RUN, ContentItemStatus.REJECTED, ContentItemStatus.EXPIRED)
    for status in succeeding:
        if status.value == value:
            return status
    raise failure(steps.WRITING_FAILED, steps.RESULT_UNREADABLE)


def _dollars(text: str) -> Decimal:
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise failure(steps.WRITING_FAILED, steps.RESULT_UNREADABLE) from None
    if not amount.is_finite() or amount < 0:
        raise failure(steps.WRITING_FAILED, steps.RESULT_UNREADABLE)
    return amount


def scope_of(run: WritingRunInput) -> dict[str, uuid.UUID]:
    """The run as the scoped functions of ``db.writing_runs`` take it."""
    return {
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "run_id": run.run_id,
    }


def subject_of(run_id: uuid.UUID) -> str:
    """How the log names the run's work."""
    return f"writing run {run_id}"


def failure(kind: str, message: str) -> ApplicationError:
    """The failure of ``kind``, which is not tried again when it cannot pass."""
    return ApplicationError(message, type=kind, non_retryable=kind in steps.NOT_RETRIED)
