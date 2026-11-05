"""What the runtime's activities do for a task: the worker's side of the task's graph.

The task's graph and its rules are core's. ``TaskWork`` is what stands between them and this
worker's database and models, one method for each thing the agent runtime asks an agent for:

- ``initial_state``: what a task starts from: its request, the installation's default scope
  of a task (the worker's settings) and when it was requested, which its deadline counts from;
- ``context``: what the nodes of one attempt are given: a chat model of the attempt's own,
  which answers the role ``orchestrator``, the reading of the project's approved profile, of
  the reports of its finished research runs and of the conversation of the chat that asked
  for the task, and the clock;
- ``record_progress``: storing the events of each node as it ends (``progress.py`` in core);
- ``confirm_plan``: the handler of the action ``confirm_plan``. It does nothing: the gate held
  the action for a person and runs it only once the plan was confirmed, which is all it is
  for; a rejection or a deadline reaches the graph as the gate's failed outcome;
- ``prepare_research``, ``prepare_studio``, ``prepare_writing``: what creates the record of
  the run a step starts, before the runtime starts its workflow and waits for it. The run is
  stored by core's ``request_*_for_action``, keyed by the action's id (asked for by a run, with
  no chat: the task reports, not the run), and the step is recorded as dispatched with it, with
  the event ``run_started`` the person watching the task is shown;
- ``finish``: storing what the task came to (its plan and each change of it, how each step's
  run ended, its report, how it came out and what its own model calls cost, the task marked
  succeeded), all in one transaction, and the small projection the workflow's ``after`` is
  handed.

``KeptSteps`` wraps the steps of the graph. Each time a step ends waiting for an action, the
plan in force, its changes and the outcomes of the steps that ran are stored from the state the
step left, so a person sees them while the task runs: the plan before they confirm it, and each
step's run as it ends. It is also what makes a step's dispatch possible, since core records a
step only of the plan it holds. And it words what a task fails with, as the writer's steps do.

Everything can run twice without doing its work twice: a plan, a change, a step's dispatch and
its outcome are each recorded once (the same again changes nothing), the run a step starts is
keyed by the action, and the report is stored once.

No credential is kept here or put in the graph's state: the model of an attempt is made for the
task's workspace (``new_model``) the first time a node asks it something, so a step that calls
no model (the dispatch of a step, or the one that resumes after a run ended days later) needs
no key. A key that is set nowhere, or a credential that cannot be read, ends the task in the
words of its error, which name what to set and hold no value.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.agent_runtime.actions import ActionOutcome
from openmarketer_core.agent_runtime.langgraph_agent import compile_agent
from openmarketer_core.agent_runtime.langgraph_steps import GraphThread
from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
from openmarketer_core.credentials.resolution import CredentialUnreadable, ModelKeyNotSet
from openmarketer_core.db.chat import chat_history
from openmarketer_core.db.media_assets import AssetNotUsable, MediaAssetNotFound
from openmarketer_core.db.models import TaskStepOutcome
from openmarketer_core.db.profile_versions import latest_approved_profile
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.research_runs import ResearchRunNotFound
from openmarketer_core.db.research_store import (
    ResearchReport,
    ResearchRunNotUsable,
    finished_research_reports,
)
from openmarketer_core.db.run_events import append_events
from openmarketer_core.db.session import transaction
from openmarketer_core.db.studio_runs import ReferenceAssetNotFound, StudioRunNotFound
from openmarketer_core.db.tasks import (
    OrchestratorTask,
    TaskFinished,
    TaskHasNoPlan,
    TaskNotFound,
    TaskNotStarted,
    TaskPlanRecorded,
    TaskStepDispatched,
    TaskStepNotDispatched,
    TaskStepNotInPlan,
    TaskStepOutcomeRecorded,
    TaskToExecute,
    mark_task_succeeded,
    orchestrator_task,
    record_plan_change,
    record_step_dispatched,
    record_step_outcome,
    record_task_plan,
    run_thread_id,
    task_to_execute,
)
from openmarketer_core.db.writing_runs import WritingRunNotFound
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import ChatModel, LLMError
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.orchestrator.task.failures import TASK_FAILURES
from openmarketer_core.orchestrator.task.plan import (
    ChatLine,
    ResearchReportAtHand,
    StoredTaskInvalid,
    TaskAgentName,
    TaskAtHand,
    TaskOutcome,
    TaskPlan,
    TaskPlanChange,
    TaskReport,
)
from openmarketer_core.orchestrator.task.task_agent.definitions import (
    ConfirmPlan,
    RunResearch,
    RunStudio,
    RunWriting,
    TaskContext,
    TaskState,
    agent,
    initial_state,
    result_of,
)
from openmarketer_core.orchestrator.task.task_agent.rules import StepStatus, TaskLimits
from openmarketer_core.orchestrator.task.workflow_contract import TASK_RUN_KIND, TaskRunInput
from openmarketer_core.research.request import request_research_for_action
from openmarketer_core.research.workflow_contract import RESEARCH_RUN_KIND
from openmarketer_core.run_events import RUN_STARTED, RunEvent
from openmarketer_core.studio.request import request_asset_for_action
from openmarketer_core.studio.workflow_contract import STUDIO_RUN_KIND
from openmarketer_core.workspace_settings.model_settings import StoredModelChoiceInvalid
from openmarketer_core.writing.request import request_post_for_action
from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND
from openmarketer_worker.agent_runtime.activities import AgentGraph
from openmarketer_worker.agent_runtime.activity_support import (
    CheckpointAccess,
    ModelOfTheAttempt,
)
from openmarketer_worker.agent_runtime.routes import ActionContext, RunContext
from openmarketer_worker.orchestrator_task import steps
from openmarketer_worker.orchestrator_task.activities import failure

logger = logging.getLogger(__name__)

# What the credentials and choices of a task's workspace are refused with: the model provider's
# key is set nowhere, a stored value does not open, or a stored model choice breaks a rule. None
# passes by itself, and each is worded for whoever asked for the task.
WORKSPACE_FAILURES = (ModelKeyNotSet, CredentialUnreadable, StoredModelChoiceInvalid)
# Why the run of a step could not be created as the step asked: the project, or the asset or
# research run its brief names, is not there or cannot be used. Each says which, by id.
NOT_CREATED = (
    ProjectNotFound,
    MediaAssetNotFound,
    AssetNotUsable,
    ResearchRunNotFound,
    ResearchRunNotUsable,
    ReferenceAssetNotFound,
)
# Why a step's run could not be recorded as the step's: the task or its plan does not hold it.
NOT_DISPATCHED = (
    TaskHasNoPlan,
    TaskStepNotInPlan,
    TaskStepDispatched,
    ResearchRunNotFound,
    StudioRunNotFound,
    WritingRunNotFound,
)
# The kind of run of each agent a step may start, as the event ``run_started`` names it.
RUN_KIND_OF = {
    TaskAgentName.RESEARCH: RESEARCH_RUN_KIND,
    TaskAgentName.STUDIO: STUDIO_RUN_KIND,
    TaskAgentName.WRITING: WRITING_RUN_KIND,
}
# What a task's planner and every review are read of the project: its latest finished reports,
# a few findings of each, and the newest part of the chat that asked for it. Core bounds what
# is put in the prompt.
SHOWN_REPORTS = 10
SHOWN_REPORT_STATEMENTS = 3
SHOWN_CHAT_MESSAGES = 40


def _report_at_hand(report: ResearchReport) -> ResearchReportAtHand:
    """A stored report as the task keeps it: its questions, a few statements, its count."""
    statements = [
        str(finding.get("statement", ""))
        for finding in report.findings
        if isinstance(finding, dict)
    ]
    return ResearchReportAtHand(
        run_id=report.run_id,
        questions=tuple(report.questions),
        statements=tuple(statements[:SHOWN_REPORT_STATEMENTS]),
        findings=len(report.findings),
        finished_at=report.created_at,
    )


class TaskWork:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        checkpoints: CheckpointAccess,
        *,
        new_model: Callable[[uuid.UUID], ChatModel],
        limits: TaskLimits,
    ) -> None:
        """``new_model`` makes the chat model of one attempt for the task's workspace.

        It may raise one of ``WORKSPACE_FAILURES``, which ends the task. ``limits`` are the
        installation's defaults of a task, which a plan keeps to unless a person confirms more.
        """
        self._sessions = sessions
        self._checkpoints = checkpoints
        self._new_model = new_model
        self._limits = limits

    # ------------------------------------------------------------------ the graph's needs
    def initial_state(self, run: GraphRunRef) -> TaskState:
        """What the graph of ``run`` starts from; its deadline counts from its request."""
        found = self._task_to_execute(run.run_id)
        return initial_state(
            TaskRunInput(
                workspace_id=found.workspace_id, project_id=found.project_id, run_id=found.run_id
            ),
            found.request,
            limits=self._limits,
            started_at=found.created_at,
        )

    def context(self, run: GraphRunRef, checkpoints: RunCheckpoints) -> TaskContext:
        """What the nodes of one attempt at a step of ``run`` are given."""
        found = self._task_to_execute(run.run_id)
        return TaskContext(
            model=ModelOfTheAttempt(lambda: self._new_model(found.workspace_id)),
            at_hand=lambda: self._at_hand(found),
            now=lambda: datetime.now(UTC),
        )

    def record_progress(self, run: GraphRunRef, events: Sequence[RunEvent]) -> None:
        """Store the events of one node of ``run``, in a short transaction of their own.

        Whatever this raises is logged by the step and never fails it (``AgentSteps``).
        """
        with transaction(self._sessions) as session:
            found = task_to_execute(session, run.run_id)
            if found is None:
                logger.info("progress of task %s not recorded: no such task", run.run_id)
                return
            append_events(
                session,
                workspace_id=found.workspace_id,
                project_id=found.project_id,
                run_kind=TASK_RUN_KIND,
                run_id=run.run_id,
                events=events,
            )

    # ------------------------------------------------------------------- the actions
    def confirm_plan(self, payload: ConfirmPlan, context: ActionContext) -> Mapping[str, Any]:
        """Nothing: the gate ran the action because a person confirmed the plan."""
        return {}

    def prepare_research(self, payload: RunResearch, context: RunContext) -> uuid.UUID:
        """Create the research run of the step, keyed by the action; record its dispatch."""

        def create() -> uuid.UUID:
            return request_research_for_action(
                self._sessions,
                **_scope(context),
                request=payload.request,
                action_id=context.action_id,
            ).id

        return self._prepare(payload, context, TaskAgentName.RESEARCH, create)

    def prepare_studio(self, payload: RunStudio, context: RunContext) -> uuid.UUID:
        """Create the studio run of the step, keyed by the action; record its dispatch."""

        def create() -> uuid.UUID:
            return request_asset_for_action(
                self._sessions,
                **_scope(context),
                request=payload.request,
                action_id=context.action_id,
            ).id

        return self._prepare(payload, context, TaskAgentName.STUDIO, create)

    def prepare_writing(self, payload: RunWriting, context: RunContext) -> uuid.UUID:
        """Create the writing run of the step, keyed by the action; record its dispatch."""

        def create() -> uuid.UUID:
            return request_post_for_action(
                self._sessions,
                **_scope(context),
                brief=payload.brief,
                action_id=context.action_id,
            ).id

        return self._prepare(payload, context, TaskAgentName.WRITING, create)

    def _prepare(
        self,
        payload: RunResearch | RunStudio | RunWriting,
        context: RunContext,
        agent_name: TaskAgentName,
        create: Callable[[], uuid.UUID],
    ) -> uuid.UUID:
        """The run of ``payload``'s step, made by ``create`` and recorded as the step's.

        A payload whose task is not the run that asks (its action's thread) is refused: a step
        is dispatched only by the task it belongs to. A run that cannot be made as asked, or
        recorded as the step's, fails the action for good, in words the graph is told.
        """
        if not context.action_id.startswith(f"{run_thread_id(payload.task_id)}:"):
            raise failure(
                steps.STEP_NOT_STARTED,
                f"step {payload.step!r} is not a step of the task that asked for it",
            )
        try:
            child_id = create()
        except NOT_CREATED as e:
            raise failure(
                steps.STEP_NOT_STARTED, f"the run of step {payload.step!r} was not created: {e}"
            ) from None
        try:
            with transaction(self._sessions) as session:
                task = orchestrator_task(session, **_scope(context), task_id=payload.task_id)
                dispatched_before = any(step.step_key == payload.step for step in task.steps)
                record_step_dispatched(
                    session,
                    **_scope(context),
                    task_id=payload.task_id,
                    step_key=payload.step,
                    agent=agent_name,
                    child_run_id=child_id,
                )
                if not dispatched_before:
                    detail = {"run_kind": RUN_KIND_OF[agent_name], "run_id": str(child_id)}
                    append_events(
                        session,
                        **_scope(context),
                        run_kind=TASK_RUN_KIND,
                        run_id=payload.task_id,
                        events=[RunEvent(RUN_STARTED, detail)],
                    )
        except (TaskNotFound, TaskFinished, TaskNotStarted) as e:
            raise failure(steps.TASK_UNAVAILABLE, str(e)) from None
        except NOT_DISPATCHED as e:
            raise failure(steps.STEP_NOT_STARTED, str(e)) from None
        return child_id

    # --------------------------------------------------------------------- the end
    def finish(self, run: GraphRunRef, final_state: Mapping[str, Any]) -> dict[str, Any]:
        """Store what the task came to and mark it succeeded; the projection ``after`` gets.

        The plan and its changes, how each dispatched step's run ended, the report, the
        outcome and what its own model calls cost, in one transaction. An outcome and amounts
        as exact text; never the plan or the report. Safe to repeat: a task that ended with
        this report is given it again without a change.
        """
        result = result_of(final_state)
        task = _task_of(final_state)
        try:
            outcome = TaskOutcome(result.outcome)
            report = TaskReport.model_validate(result.report)
        except (ValueError, ValidationError):
            logger.exception("task %s finished with a result that does not fit", task.run_id)
            raise failure(steps.TASK_FAILED, steps.RESULT_UNREADABLE) from None
        cost = _dollars(result.cost_usd)
        try:
            with transaction(self._sessions) as session:
                keep_what_ran(session, task, final_state)
                mark_task_succeeded(
                    session,
                    workspace_id=task.workspace_id,
                    project_id=task.project_id,
                    task_id=task.run_id,
                    outcome=outcome,
                    report=report,
                    cost_usd=cost,
                )
        except (TaskNotFound, TaskFinished, TaskNotStarted, ProjectNotFound) as e:
            raise failure(steps.TASK_UNAVAILABLE, str(e)) from None
        except (
            TaskHasNoPlan,
            TaskPlanRecorded,
            TaskStepNotDispatched,
            TaskStepOutcomeRecorded,
        ) as e:
            raise failure(steps.TASK_FAILED, str(e)) from None
        return {
            "outcome": outcome.value,
            "cost_usd": str(cost),
            "total_cost_usd": str(_dollars(result.total_cost_usd)),
        }

    def spent(self, run: TaskRunInput) -> Decimal:
        """What the task's own model calls cost, as its last checkpoint counted it; 0 without one.

        Read while no attempt holds the task's checkpoints: ``AttemptInProgress`` otherwise.
        """
        with self._checkpoints.of_attempt(run_thread_id(run.run_id)) as checkpoints:
            values = state_of(checkpoints)
        return _dollars(values.get("cost_usd", 0.0))

    # ------------------------------------------------------------------- reading
    def _at_hand(self, task: TaskToExecute) -> TaskAtHand:
        """The project as the task finds it, read once: profile, reports and asking chat."""
        with transaction(self._sessions) as session:
            approved = latest_approved_profile(
                session, workspace_id=task.workspace_id, project_id=task.project_id
            )
            reports = finished_research_reports(
                session,
                workspace_id=task.workspace_id,
                project_id=task.project_id,
                limit=SHOWN_REPORTS,
            )
            messages = (
                ()
                if task.chat_thread_id is None
                else chat_history(
                    session,
                    workspace_id=task.workspace_id,
                    project_id=task.project_id,
                    chat_id=task.chat_thread_id,
                )
            )
        return TaskAtHand(
            profile=None if approved is None else approved.profile,
            reports=tuple(_report_at_hand(report) for report in reports),
            chat=tuple(
                ChatLine(role=message.role.value, content=message.content)
                for message in messages[-SHOWN_CHAT_MESSAGES:]
            ),
        )

    def _task_to_execute(self, task_id: uuid.UUID) -> TaskToExecute:
        try:
            with transaction(self._sessions) as session:
                found = task_to_execute(session, task_id)
        except StoredTaskInvalid as e:
            raise failure(steps.TASK_FAILED, str(e)) from None
        if found is None:
            raise failure(steps.TASK_UNAVAILABLE, f"there is no task {task_id}")
        return found


class KeptSteps:
    """The steps of the task's graph, with what each left stored and its failures worded."""

    def __init__(self, graph: AgentGraph, sessions: sessionmaker[Session]) -> None:
        self._graph = graph
        self._sessions = sessions

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        """Take the step, as ``AgentGraph.advance`` says; then keep what the step left.

        A step that waits for an action stores the plan in force, its changes and the outcomes
        of the steps that ran, from the state it checkpointed, before the action runs. A plan
        or answers of the model that cannot be read, a model whose key is not set and a
        configuration that refuses the call end the task (``TASK_FAILED``); only the model
        provider fails in ways that pass, an outage or a rate limit (``MODEL_UNAVAILABLE``,
        tried again). The message is the error's own, which names no value.
        """
        try:
            step = self._graph.advance(run, checkpoints, resume)
        except (*TASK_FAILURES, StoredTaskInvalid, *WORKSPACE_FAILURES, ConfigError) as e:
            logger.info("task %s failed: %s", run.run_id, e)
            passing = isinstance(e, LLMError) and e.may_pass
            kind = steps.MODEL_UNAVAILABLE if passing else steps.TASK_FAILED
            raise failure(kind, str(e)) from None
        if step.action is not None:
            self._keep(state_of(checkpoints))
        return step

    def _keep(self, values: Mapping[str, Any]) -> None:
        if not values.get("plan"):
            return
        task = _task_of(values)
        try:
            with transaction(self._sessions) as session:
                keep_what_ran(session, task, values)
        except (TaskNotFound, TaskFinished, TaskNotStarted, ProjectNotFound) as e:
            raise failure(steps.TASK_UNAVAILABLE, str(e)) from None
        except (TaskHasNoPlan, TaskPlanRecorded, TaskStepOutcomeRecorded) as e:
            raise failure(steps.TASK_FAILED, str(e)) from None


def keep_what_ran(session: Session, task: TaskRunInput, values: Mapping[str, Any]) -> None:
    """Store, from the graph's state, the plan in force, its changes and the steps' outcomes.

    What is stored already is kept: the plans of the state are, in order, the one each change
    replaced and the one in force, and only the changes the task does not hold yet are added.
    A step's outcome is stored once its step was dispatched and its run has ended.
    """
    scope = {"workspace_id": task.workspace_id, "project_id": task.project_id}
    changes = [TaskPlanChange.model_validate(change) for change in values["plan_history"]]
    plans = [*(change.replaced for change in changes), TaskPlan.model_validate(values["plan"])]
    stored = orchestrator_task(session, **scope, task_id=task.run_id)
    if stored.plan is None:
        stored = record_task_plan(session, **scope, task_id=task.run_id, plan=plans[0])
    for change, plan in list(zip(changes, plans[1:], strict=True))[len(stored.plan_changes) :]:
        stored = record_plan_change(
            session, **scope, task_id=task.run_id, plan=plan, reason=change.reason
        )
    _keep_outcomes(session, scope, stored, values["steps"])


def _keep_outcomes(
    session: Session,
    scope: Mapping[str, uuid.UUID],
    task: OrchestratorTask,
    records: Sequence[Mapping[str, Any]],
) -> None:
    pending = {step.step_key for step in task.steps if step.outcome is None}
    for record in records:
        outcome = step_run_outcome(record)
        if record["id"] in pending and outcome is not None:
            record_step_outcome(
                session, **scope, task_id=task.id, step_key=record["id"], outcome=outcome
            )


def step_run_outcome(record: Mapping[str, Any]) -> TaskStepOutcome | None:
    """How the run of a step of the state ended, or ``None`` while it has not.

    A studio run that ended waiting for a person's review needs review; any other run that
    ended succeeded; a step that failed did, whether its run did not finish or could not be
    started (a step that started no run was never dispatched, and is not recorded). A step
    that was dropped started no run.
    """
    status = record.get("status")
    if status == StepStatus.FAILED:
        return TaskStepOutcome.FAILED
    if status != StepStatus.SUCCEEDED:
        return None
    outcome = record.get("outcome") or {}
    if record.get("agent") == TaskAgentName.STUDIO and outcome.get("needs_review") is True:
        return TaskStepOutcome.NEEDS_REVIEW
    return TaskStepOutcome.SUCCEEDED


def state_of(checkpoints: RunCheckpoints) -> dict[str, Any]:
    """The latest values of the task's state in ``checkpoints``; {} for a thread with none."""
    thread = GraphThread(compile_agent(agent, checkpoints.store), checkpoints, recursion_limit=1)
    return thread.state().values


def _task_of(values: Mapping[str, Any]) -> TaskRunInput:
    return TaskRunInput(
        workspace_id=uuid.UUID(values["workspace_id"]),
        project_id=uuid.UUID(values["project_id"]),
        run_id=uuid.UUID(values["run_id"]),
    )


def _scope(context: RunContext) -> dict[str, uuid.UUID]:
    return {
        "workspace_id": context.scope.workspace_id,
        "project_id": context.scope.project_id,
    }


def _dollars(amount: object) -> Decimal:
    """An amount the graph counted, to a millionth of a dollar, never below 0."""
    try:
        value = Decimal(str(amount))
    except ArithmeticError:
        return Decimal(0)
    if not value.is_finite() or value < 0:
        return Decimal(0)
    return value.quantize(Decimal("0.000001"))
