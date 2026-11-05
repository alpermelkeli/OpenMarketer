"""The task's declarations: the agent, its state, what its nodes are given, and its actions.

This is the module the worker's workflow loads, so it imports no graph framework and nothing
that touches the database. The nodes are in ``graph.py``, which only the process that runs the
graph imports; importing it is what puts them on ``agent``.

- ``TaskState`` is what the graph keeps between steps and what a checkpoint stores: plain data
  only. The limits (the installation's defaults, then the accepted plan's scope), the cost of
  the task's own model calls and the number of plan changes are in it, so they count every
  attempt of a task.
- ``TaskContext`` is what a node needs that is not state: the model of the attempt, which
  answers the role ``orchestrator``; one reading of the project as the task finds it (its
  approved profile, its finished reports and the chat that asked), which ``prepare`` calls
  once; and the clock the deadline is read by.
- The actions. ``confirm_plan`` asks a person to confirm a plan that can spend or whose scope
  is above the defaults: its effect is ``CONFIRMS``, so the gate holds it as a proposal exactly
  as it holds an action that publishes; its payload is the plan itself, so what the person
  confirms is in the proposal, and the number of the confirmation, so a confirmation asked
  after a change is a request of its own. Its handler does nothing: being approved is all it
  is for. ``run_research``, ``run_studio`` and ``run_writing`` run another agent's run and
  wait for it (``RunKind.WAIT``): the worker creates the child's record from the payload (who
  asked: a run; no chat thread), the child's workflow runs, and its final state is the outcome.
  Each payload names the task and the step and carries the request in the agent's own schema,
  its references already resolved by code: every id in it was written by code, never by the
  model. There is no other action.
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from openmarketer_core.agent_runtime.actions import ActionEffect, ChildRun, RunKind, RunTarget
from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_core.llm import ChatModel
from openmarketer_core.orchestrator.task.plan import (
    StepId,
    TaskAtHand,
    TaskPlan,
    TaskRequest,
)
from openmarketer_core.orchestrator.task.task_agent.rules import TaskLimits
from openmarketer_core.orchestrator.task.workflow_contract import TASK_RUN_KIND, TaskRunInput
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.workflow_contract import (
    RESEARCH_LONGEST_RUN_SECONDS,
    RESEARCH_TASK_QUEUE,
    RESEARCH_WORKFLOW,
    ResearchRunInput,
)
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.studio.workflow_contract import (
    STUDIO_LONGEST_RUN_SECONDS,
    STUDIO_TASK_QUEUE,
    STUDIO_WORKFLOW,
    StudioRunInput,
)
from openmarketer_core.writing.brief import WritingBrief
from openmarketer_core.writing.workflow_contract import (
    WRITING_LONGEST_RUN_SECONDS,
    WRITING_TASK_QUEUE,
    WRITING_WORKFLOW,
    WritingRunInput,
)

CONFIRM_PLAN = "confirm_plan"
RUN_RESEARCH = "run_research"
RUN_STUDIO = "run_studio"
RUN_WRITING = "run_writing"

# How confirming a plan is tried: its handler records nothing and changes nothing, so a repeat
# is harmless. How long it waits for a person is the gate's default, three days.
CONFIRM_TIMEOUT_SECONDS = 30
CONFIRM_ATTEMPTS = 3
CONFIRM_RETRY_AFTER_SECONDS = 5


class TaskState(TypedDict):
    # The task, as text because a checkpoint stores plain data.
    workspace_id: str
    project_id: str
    run_id: str
    # What it was asked (``plan.TaskRequest``, dumped as JSON), when it started (ISO 8601), and
    # the installation's defaults of a task (``rules.TaskLimits``).
    request: dict[str, Any]
    started_at: str
    limits: dict[str, Any]
    # What the task found at the project when it started (``rules.at_hand_data``): the approved
    # profile as ``rules.project_of`` reads it ({} without one), the reports of its finished
    # research runs (``rules.reports_of``; each labelled ``report-1`` ... and the run's id kept
    # in the state) and the conversation of the chat that asked (``rules.chat_of``).
    at_hand: dict[str, Any]
    # The plan in force (``plan.TaskPlan`` as JSON; {} until one is accepted), the plan the
    # last plan or review submitted for ``check`` ({} once checked), how often it changed, and
    # whether the plan in force waits for a person (and how often one was asked).
    plan: dict[str, Any]
    proposed_plan: dict[str, Any]
    plan_changes: int
    # Each change of plan: {"reason", "replaced"} (``plan.TaskPlanChange`` as JSON), oldest first.
    plan_history: list[dict[str, Any]]
    needs_confirmation: bool
    confirmations: int
    confirmation: str  # "" | "confirmed" | "rejected" | "expired": the latest answer
    # The steps of the plan as they ran: {"id", "agent", "status", "run_id", "outcome",
    # "error"} (``rules.steps_of``), and the step that finished last ("" before any).
    steps: list[dict[str, Any]]
    finished_step: str
    review: dict[str, Any]  # the latest review (``rules.Review`` as JSON), {} before any
    next: str  # what the latest review or check chose: "dispatch", "check" or "report"
    outcome: str  # a ``rules.TaskOutcome``'s value once reported, "" before
    report: dict[str, Any]  # ``plan.TaskReport`` as JSON once reported, {} before
    total_cost_usd: float  # its own model calls and its runs', once reported
    notes: list[str]  # what the code dropped or worked around
    cost_usd: float  # the task's own model calls
    model: str


@dataclass
class TaskContext:
    """What the nodes of one attempt are given besides the state.

    ``model`` answers the role ``orchestrator``. ``at_hand`` reads the project once: its
    approved profile, the reports of its finished research runs, newest first, and the
    conversation of the chat that asked for the task, oldest first. Only ``prepare`` calls it.
    ``now`` is the time, aware, for the deadline. A context serves one attempt, in one thread.
    """

    model: ChatModel
    at_hand: Callable[[], TaskAtHand]
    now: Callable[[], datetime]


agent = Agent(TASK_RUN_KIND, state=TaskState, context=TaskContext)


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


# What each child workflow is started with: its run's ids, never its request, which its
# record holds. Plain functions at the top of a module the workflow sandbox loads.
def research_run_input(child: ChildRun) -> ResearchRunInput:
    return ResearchRunInput(child.workspace_id, child.project_id, child.run_id)


def studio_run_input(child: ChildRun) -> StudioRunInput:
    return StudioRunInput(child.workspace_id, child.project_id, child.run_id)


def writing_run_input(child: ChildRun) -> WritingRunInput:
    return WritingRunInput(child.workspace_id, child.project_id, child.run_id)


@agent.action(
    CONFIRM_PLAN,
    timeout=CONFIRM_TIMEOUT_SECONDS,
    attempts=CONFIRM_ATTEMPTS,
    retry_after=CONFIRM_RETRY_AFTER_SECONDS,
    # A person confirms the plan before anything of it runs: the gate holds it.
    effect=ActionEffect.CONFIRMS,
)
class ConfirmPlan(_Payload):
    """Ask a person to confirm the task's plan: the plan itself, and which confirmation it is."""

    task_id: uuid.UUID
    confirmation: int = Field(ge=1)
    plan: TaskPlan


@agent.action(
    RUN_RESEARCH,
    run=RunTarget(
        workflow=RESEARCH_WORKFLOW,
        task_queue=RESEARCH_TASK_QUEUE,
        kind=RunKind.WAIT,
        longest_seconds=RESEARCH_LONGEST_RUN_SECONDS,
        input_of=research_run_input,
    ),
)
class RunResearch(_Payload):
    """Run a research run for step ``step`` of the task, and wait for it."""

    task_id: uuid.UUID
    step: StepId
    request: ResearchRequest


@agent.action(
    RUN_STUDIO,
    run=RunTarget(
        workflow=STUDIO_WORKFLOW,
        task_queue=STUDIO_TASK_QUEUE,
        kind=RunKind.WAIT,
        longest_seconds=STUDIO_LONGEST_RUN_SECONDS,
        input_of=studio_run_input,
    ),
)
class RunStudio(_Payload):
    """Run a studio run for step ``step`` of the task, and wait for it. The run spends under
    its own actions, which the gate weighs; starting it spends nothing."""

    task_id: uuid.UUID
    step: StepId
    request: AssetRequest


@agent.action(
    RUN_WRITING,
    run=RunTarget(
        workflow=WRITING_WORKFLOW,
        task_queue=WRITING_TASK_QUEUE,
        kind=RunKind.WAIT,
        longest_seconds=WRITING_LONGEST_RUN_SECONDS,
        input_of=writing_run_input,
    ),
)
class RunWriting(_Payload):
    """Run a writing run for step ``step`` of the task, and wait for it, its post's decision
    included. The brief's asset and research run are the ids code resolved."""

    task_id: uuid.UUID
    step: StepId
    brief: WritingBrief


def initial_state(
    run: TaskRunInput, request: TaskRequest, *, limits: TaskLimits, started_at: datetime
) -> TaskState:
    """What a task starts from: the task, its request, its limits, and nothing done yet.

    ``started_at`` is when the task was created, aware; its deadline counts from it.
    """
    if started_at.tzinfo is None:
        raise ValueError("started_at must be an aware time")
    return {
        "workspace_id": str(run.workspace_id),
        "project_id": str(run.project_id),
        "run_id": str(run.run_id),
        "request": request.model_dump(mode="json"),
        "started_at": started_at.isoformat(),
        "limits": limits.as_data(),
        "at_hand": {"project": {}, "research_reports": [], "chat": []},
        "plan": {},
        "proposed_plan": {},
        "plan_changes": 0,
        "plan_history": [],
        "needs_confirmation": False,
        "confirmations": 0,
        "confirmation": "",
        "steps": [],
        "finished_step": "",
        "review": {},
        "next": "",
        "outcome": "",
        "report": {},
        "total_cost_usd": 0.0,
        "notes": [],
        "cost_usd": 0.0,
        "model": "",
    }


@dataclass(frozen=True)
class TaskResult:
    """What a task whose graph finished hands to whoever stores it."""

    outcome: str
    report: dict[str, Any]
    plan: dict[str, Any]
    steps: list[dict[str, Any]]
    plan_changes: int
    plan_history: list[dict[str, Any]]  # ``plan.TaskPlanChange`` as JSON, oldest first
    cost_usd: float  # its own model calls
    total_cost_usd: float  # with every run it started
    model: str
    notes: list[str]


def result_of(final_state: Mapping[str, Any]) -> TaskResult:
    """What a task whose graph finished produced, as ``report`` left it."""
    return TaskResult(
        outcome=final_state["outcome"],
        report=dict(final_state["report"]),
        plan=dict(final_state["plan"]),
        steps=[dict(step) for step in final_state["steps"]],
        plan_changes=final_state["plan_changes"],
        plan_history=[dict(change) for change in final_state["plan_history"]],
        cost_usd=final_state["cost_usd"],
        total_cost_usd=final_state["total_cost_usd"],
        model=final_state["model"],
        notes=list(final_state["notes"]),
    )
