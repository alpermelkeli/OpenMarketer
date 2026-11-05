"""What an orchestrator task is asked, the plan it carries out, and the report it ends with.

A task is asked by the chat's request action: the operator's request in their own words and
the chat's one-paragraph understanding of it (``TaskRequest``). The orchestrator answers with a
plan as structured output (``TaskPlan``): a goal, steps, and the scope the task keeps to. Each
step is one run of an agent the task may start, with its request, the steps it waits for
(``depends_on``) and, for a writing step, the earlier steps whose results its brief takes.

A step's request is the agent's own schema read just as strictly, minus what a reference
replaces: a model never writes an id. So a writing step has no ``asset`` and no
``research_run_id``; it names a studio step in ``asset_from_step`` and a research step in
``research_from_step``, and code puts the ids of their results in the brief when the step is
dispatched (``WritingStepRequest.brief_with``). A studio step names no reference assets: its
style refers to nothing. A research step's request is a ``ResearchRequest`` as it is.

This module holds the schemas only. Whether a reference points backwards to a step of the
right agent, whether ``depends_on`` names steps of the plan, whether the plan keeps to the
limits of a task and when it needs a person's confirmation are the task agent's rules
(``task_agent/rules.py``). Every field is text a model wrote, so every field is bounded and
nothing is read leniently. This module reaches no database and no network.
"""

from __future__ import annotations

import uuid
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

from openmarketer_core.profile import FeatureId
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
    PLATFORMS_WITH_IMAGE,
    Angle,
    WritingBrief,
    WritingGoal,
    WritingPlatform,
)

MAX_REQUEST_CHARS = 2000
MAX_UNDERSTANDING_CHARS = 1000
MAX_TASK_GOAL_CHARS = 500
MAX_STEP_ID_CHARS = 32
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
# What a writing step's request is checked with in place of the image its reference gives.
_STAND_IN_ASSET = uuid.UUID(int=0)

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
        # The brief's own rules (the fields of each goal) hold for the step as they are. Its
        # image comes from a reference, so it stands in for one here; that a step for a
        # platform with an image has its reference is the task agent's rule.
        stand_in = _STAND_IN_ASSET if self.platform in PLATFORMS_WITH_IMAGE else None
        self.brief_with(asset=stand_in, research_run_id=None)
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
    ``research_from_step`` a research step, whose findings the writer may quote.
    """

    model_config = ConfigDict(extra="forbid")

    id: StepId
    agent: Literal["writing"]
    request: WritingStepRequest
    depends_on: StepIds = Field(default_factory=list)
    asset_from_step: StepId | None = None
    research_from_step: StepId | None = None


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
            raise ValueError("a step's id is used once in a plan")
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
