"""What an orchestrator task is asked, the plan it carries out, and the report it ends with.

A task is asked by the chat's request action: the operator's request in their own words and
the chat's one-paragraph understanding of it (``TaskRequest``). The orchestrator answers with a
plan as structured output (``TaskPlan``): a goal, steps, and the scope the task keeps to. Each
step is one run of an agent the task may start, with its request, the steps it waits for
(``depends_on``) and, for a writing step, the earlier steps whose results its brief takes.

A step's request is the agent's own schema read just as strictly, minus what a reference
replaces: a model never writes an id. So a writing step has no ``asset`` and no
``research_run_id``; it names a studio step in ``asset_from_step``, a research step in
``research_from_step``, or one of the project's finished reports in
``research_from_report`` (``report-1``, ``report-2`` ..., the labels code gives the reports it
puts at hand), and code puts the ids of their results in the brief when the step is dispatched
(``WritingStepRequest.brief_with``). A studio step names no reference assets: its style refers
to nothing. A research step's request is a ``ResearchRequest`` as it is.

What the project already has is read once, as a ``TaskAtHand``: the approved profile, the
reports of its finished research runs and the conversation of the chat that asked for the
task. Code gives the reports their labels and builds what the model is shown, so the planner
chooses among labels rather than ids (``task_agent/rules.py``).

This module holds the schemas only. Whether a reference points backwards to a step of the
right agent, whether ``depends_on`` names steps of the plan, whether the plan keeps to the
limits of a task and when it needs a person's confirmation are the task agent's rules
(``task_agent/rules.py``). Every field is text a model wrote, so every field is bounded and
nothing is read leniently. This module reaches no database and no network.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from openmarketer_core.agent_runtime.model_input import code_rule
from openmarketer_core.profile import FeatureId, ProductProfile
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import (
    MAX_COPY_LINES,
    AssetRequest,
    CopyLine,
    Goal,
    StyleNotes,
)
from openmarketer_core.writing.brief import (
    DEFAULT_VARIANTS,
    MAX_VARIANTS,
    Angle,
    WritingBrief,
    WritingGoal,
    WritingPlatform,
    brief_rule_broken,
)

# The operator's words are copied verbatim, so they may be as long as a chat message (the API's
# MAX_MESSAGE_LENGTH); a shorter limit refused a long message the model could not shorten.
MAX_REQUEST_CHARS = 10_000
# One paragraph of what the operator wants and what the project's records add to it: 1000
# characters was too few for that, and the chat sent the same longer paragraph again and again.
MAX_UNDERSTANDING_CHARS = 3000
MAX_TASK_GOAL_CHARS = 500
MAX_STEP_ID_CHARS = 32
MAX_REPORT_LABEL_CHARS = 16
MAX_REPORT_CHARS = 4000
MAX_CHANGE_REASON_CHARS = 500
MAX_REPORT_ITEMS = 50
# The ceilings of a task's scope. A plan may ask for less, and its check compares it with the
# installation's defaults (the worker's settings); nothing may ask for more than these.
MAX_TASK_STEPS = 12
MAX_TASK_PLAN_CHANGES = 5
MAX_TASK_DEADLINE_DAYS = 30
MAX_TASK_COST_USD = Decimal(20)

# The fields of each agent's schema that a step's request does not carry, because a reference
# replaces them or because they are ids a model would write. A test holds the step requests to
# their schemas minus these.
WRITING_FIELDS_REPLACED = frozenset({"asset", "research_run_id"})
STUDIO_FIELDS_REPLACED = frozenset({"reference_asset_ids"})

Request = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_REQUEST_CHARS)
]
Understanding = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_UNDERSTANDING_CHARS),
]
TaskGoal = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TASK_GOAL_CHARS)
]
# A step's name within its plan, such as ``image`` or ``post-1``: a label the plan chooses,
# never the id of a record.
StepId = Annotated[str, Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=MAX_STEP_ID_CHARS)]
# One of the labels code gives the project's finished research reports, such as ``report-1``.
ReportLabel = Annotated[
    str, Field(pattern=r"^report-[1-9][0-9]*$", max_length=MAX_REPORT_LABEL_CHARS)
]
ChangeReason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_CHANGE_REASON_CHARS),
]
ReportText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_REPORT_CHARS)
]


def _distinct[T](items: list[T]) -> list[T]:
    if len(set(items)) != len(items):
        raise ValueError("an item is named once")
    return items


StepIds = Annotated[list[StepId], Field(max_length=MAX_TASK_STEPS), AfterValidator(_distinct)]


class TaskRequest(BaseModel):
    """What a task is asked: the operator's words, and the chat's understanding of them."""

    model_config = ConfigDict(extra="forbid")

    request: Request
    understanding: Understanding


@dataclass(frozen=True)
class ResearchReportAtHand:
    """One finished research run's report the task may rest a post on.

    The run's id is kept in the task's state and never shown to the model; the label the model
    names is code's (``rules.reports_of``). ``statements`` is a few of the report's findings,
    enough for the planner to judge whether the report bears on the request.
    """

    run_id: uuid.UUID
    questions: tuple[str, ...]
    statements: tuple[str, ...]
    findings: int
    finished_at: datetime


@dataclass(frozen=True)
class ChatLine:
    """One message of the chat that asked for the task, as the planner reads it."""

    role: str
    content: str


@dataclass(frozen=True)
class TaskAtHand:
    """The project as the task reads it once, for the plan and every review.

    The worker fills it from the project's records in one read: the approved profile, the
    reports of its finished research runs, newest first, and the conversation of the chat that
    asked for the task, oldest first. Only ``prepare`` reads it; what the model is shown is
    built from it by code (``rules.at_hand_data``).
    """

    profile: ProductProfile | None
    reports: tuple[ResearchReportAtHand, ...]
    chat: tuple[ChatLine, ...]


class TaskAgentName(StrEnum):
    """The agents a task may start, one run per step: the ``agent`` of each kind of step."""

    RESEARCH = "research"
    WRITING = "writing"
    STUDIO = "studio"


class StudioStepRequest(BaseModel):
    """A studio step's request: an ``AssetRequest`` without reference assets."""

    model_config = ConfigDict(extra="forbid")

    goal: Goal
    exact_copy: list[CopyLine] = Field(default_factory=list, max_length=MAX_COPY_LINES)
    platform: Platform
    style_notes: StyleNotes = ""

    def asset_request(self) -> AssetRequest:
        """The request the studio run is asked, read again by its own schema."""
        return AssetRequest.model_validate(self.model_dump(mode="json"))


class WritingStepRequest(BaseModel):
    """A writing step's request: a ``WritingBrief`` without the ids its references replace."""

    model_config = ConfigDict(extra="forbid")

    platform: WritingPlatform
    goal: WritingGoal
    feature: FeatureId | None = None
    angle: Angle | None = None
    variants: int = Field(default=DEFAULT_VARIANTS, ge=1, le=MAX_VARIANTS)

    @model_validator(mode="after")
    def _fits_a_brief(self) -> WritingStepRequest:
        # The brief's own rules (the fields of each goal) hold for the step as they are, in
        # the brief's words. Its image comes from a reference, so it counts as given here; that
        # a step for a platform with an image has its reference is the task agent's rule.
        broken = brief_rule_broken(
            self.platform,
            self.goal,
            has_feature=self.feature is not None,
            has_angle=self.angle is not None,
            has_asset=True,
        )
        if broken is not None:
            raise code_rule(broken)
        return self

    def brief_with(
        self, *, asset: uuid.UUID | None, research_run_id: uuid.UUID | None
    ) -> WritingBrief:
        """The brief the writing run is asked, with the ids code resolved from the references."""
        return WritingBrief.model_validate(
            {
                **self.model_dump(mode="json"),
                "asset": None if asset is None else str(asset),
                "research_run_id": None if research_run_id is None else str(research_run_id),
            }
        )


class ResearchStep(BaseModel):
    """A step that runs the researcher."""

    model_config = ConfigDict(extra="forbid")

    id: StepId
    agent: Literal["research"]
    request: ResearchRequest
    depends_on: StepIds = Field(default_factory=list)


class StudioStep(BaseModel):
    """A step that runs the creative studio. Its request refers to no other step."""

    model_config = ConfigDict(extra="forbid")

    id: StepId
    agent: Literal["studio"]
    request: StudioStepRequest
    depends_on: StepIds = Field(default_factory=list)


class WritingStep(BaseModel):
    """A step that runs the writer, with the earlier steps whose results its brief takes.

    ``asset_from_step`` names a studio step, whose asset the post goes with;
    ``research_from_step`` a research step, whose findings the writer may quote; and
    ``research_from_report`` a report already finished in the project. A step rests on one
    research source: it names a research step or a report, never both.
    """

    model_config = ConfigDict(extra="forbid")

    id: StepId
    agent: Literal["writing"]
    request: WritingStepRequest
    depends_on: StepIds = Field(default_factory=list)
    asset_from_step: StepId | None = None
    research_from_step: StepId | None = None
    research_from_report: ReportLabel | None = None

    @model_validator(mode="after")
    def _one_research_reference(self) -> WritingStep:
        if self.research_from_step is not None and self.research_from_report is not None:
            raise code_rule(
                "a writing step names its research once: research_from_step or "
                "research_from_report, not both"
            )
        return self


TaskStep = Annotated[ResearchStep | StudioStep | WritingStep, Field(discriminator="agent")]


class TaskScope(BaseModel):
    """What a task keeps to: its steps, how often its plan may change, its deadline and cost.

    ``max_cost_usd`` covers the task's own model calls; each run it starts keeps its own caps.
    """

    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(ge=1, le=MAX_TASK_STEPS)
    max_plan_changes: int = Field(ge=0, le=MAX_TASK_PLAN_CHANGES)
    deadline_days: int = Field(ge=1, le=MAX_TASK_DEADLINE_DAYS)
    max_cost_usd: Decimal = Field(ge=0, le=MAX_TASK_COST_USD, decimal_places=2)


class TaskPlan(BaseModel):
    """The plan of a task: its goal, its steps in order, and its scope."""

    model_config = ConfigDict(extra="forbid")

    goal: TaskGoal
    steps: list[TaskStep] = Field(min_length=1, max_length=MAX_TASK_STEPS)
    scope: TaskScope

    @model_validator(mode="after")
    def _steps_named_once(self) -> TaskPlan:
        if len({step.id for step in self.steps}) != len(self.steps):
            raise code_rule("a step's id is used once in a plan")
        return self


class TaskOutcome(StrEnum):
    """How a task that ended with its report came out; recorded with the report."""

    COMPLETED = "completed"  # every step of its plan ran
    INCOMPLETE = "incomplete"  # it stopped short of its plan: a limit, a failed step, a deadline
    PLAN_REJECTED = "plan_rejected"  # a person rejected the plan it asked to have confirmed
    PLAN_EXPIRED = "plan_expired"  # nobody confirmed its plan in time


class TaskPlanChange(BaseModel):
    """One change of a task's plan: why it was changed, and the plan it replaced."""

    model_config = ConfigDict(extra="forbid")

    reason: ChangeReason
    replaced: TaskPlan


class TaskReport(BaseModel):
    """What a finished task did, as code wrote it from its steps' outcomes.

    ``summary`` is the text the chat's follow-up turn is given; the ids link to what the
    task's runs made: the posts (content items), the assets and the research runs whose
    reports it used. ``rejected`` says, a sentence each, what a person turned down.
    """

    model_config = ConfigDict(extra="forbid")

    summary: ReportText
    content_item_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_REPORT_ITEMS)
    asset_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_REPORT_ITEMS)
    research_run_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_REPORT_ITEMS)
    rejected: list[ReportText] = Field(default_factory=list, max_length=MAX_REPORT_ITEMS)


class StoredTaskInvalid(Exception):
    """What is stored with a task (its request, plan or report) does not fit its schema."""

    def __init__(self, task_id: uuid.UUID) -> None:
        super().__init__(f"what is stored with task {task_id} does not fit its schema")
        self.task_id = task_id
