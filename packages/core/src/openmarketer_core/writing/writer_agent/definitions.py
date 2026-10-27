"""The writer's declarations: the agent, its state, what its nodes are given, and its action.

This is the module the worker's workflow loads, so it imports no graph framework and nothing
that touches the database. The nodes are in ``graph.py``, which only the process that runs the
graph imports; importing it is what puts them on ``agent``.

- ``WriterState`` is what the graph keeps between steps and what a checkpoint stores: plain
  data only. ``prepare`` copies what the run writes from (the writing context) out of the
  approved profile into it, with the version's id, so every later step and every later attempt
  writes from that version, whatever a person approves meanwhile.
- ``WriterContext`` is what a node needs that is not state: the model of the attempt, which
  answers the roles ``writer`` and ``critic``, the limits, and the project's approved profile,
  read only when ``prepare`` asks for it.
- The two actions. ``StoreDraft`` is internal: it stores the draft the person will review as
  the run's content item, ``proposed``, and answers ``{"content_item_id"}``; storing the same
  draft again gives back the stored item, so repeating it is harmless (keyed by the action's
  id). Its payload carries the draft, model-written text included, as the studio's operations
  carry a prompt: every field is bounded, and the whole weighs ``rules.MAX_DRAFT_BYTES`` at
  most, because every payload enters the workflow's history. ``PublishPost`` publishes, so the
  policy gate holds it as a proposal until a person approves or rejects it; its payload names
  the content item and carries the text, so what the person approves is in the proposal
  itself. In the first step of the writer its handler records the post as it would have been
  sent (``dry_run``) and sends nothing; repeating it is harmless, keyed by the action's id.
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from openmarketer_core.agent_runtime.actions import ActionEffect
from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_core.llm import ChatModel
from openmarketer_core.profile import FeatureId, ProductProfile
from openmarketer_core.writing.brief import WritingBrief
from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND, WritingRunInput
from openmarketer_core.writing.writer_agent import rules
from openmarketer_core.writing.writer_agent.rules import (
    Limits,
    Plan,
    PostStatus,
    Variant,
    VariantReview,
    WritingResult,
)

STORE_DRAFT = "store_draft"
PUBLISH_POST = "publish_post"

# How the actions are tried. Constants of the actions and not settings: storing a draft and
# recording a dry run are each one short write, which three attempts five seconds apart ride
# out; each handler keys its write so that a repeat writes once.
WRITE_TIMEOUT_SECONDS = 60
WRITE_ATTEMPTS = 3
WRITE_RETRY_AFTER_SECONDS = 5


class WriterState(TypedDict):
    # The run, as text because a checkpoint stores plain data.
    workspace_id: str
    project_id: str
    run_id: str
    # The brief, as it was stored with the run (``WritingBrief``, dumped as JSON).
    brief: dict[str, Any]
    # The approved profile version the run writes from, and what it writes from in it
    # (``rules.writing_context``); "" and {} until ``prepare``.
    profile_version_id: str
    writing_context: dict[str, Any]
    # The plan as code held it ({} until ``plan``), and what the code changed in it.
    plan: dict[str, Any]
    plan_changes: list[str]
    round: int  # 0 until the first draft, then the round being checked
    # This round: the variants as written, their code-rule reports (``rules.check_variant``),
    # and the critic's report ({} until it reviewed, and when it was not asked or not read).
    variants: list[dict[str, Any]]
    checks: list[dict[str, Any]]
    critique: dict[str, Any]
    # The rounds before this one: {"round", "variants", "checks", "critique"}.
    earlier_rounds: list[dict[str, Any]]
    decision: str  # what the last decide chose: "propose" or "revise"
    reason: str  # the code's sentence of why
    chosen: int  # the proposed variant's index in this round, -1 until one is chosen
    passed_critic: bool
    # The draft as ``store_draft`` stored it (``rules.draft_of``, fitted to its size), and the
    # content item it was stored as; {} and "" until then.
    draft: dict[str, Any]
    content_item_id: str
    status: str  # a ``PostStatus``'s value once publish_post's outcome came, "" before
    action_error: str  # what the runtime said of an action that did not succeed
    result: dict[str, Any]  # what ``record`` hands to whoever stores the run; {} until then
    notes: list[str]  # what the code dropped or worked around
    cost_usd: float
    model: str


@dataclass(frozen=True)
class ApprovedProfile:
    """The project's approved Product Profile, and the id of its version."""

    version_id: uuid.UUID
    profile: ProductProfile


@dataclass
class WriterContext:
    """What the nodes of one attempt are given besides the state.

    ``model`` answers the roles ``writer`` and ``critic`` (the router resolves each to its own
    model). ``approved_profile`` reads the project's approved profile, or gives ``None`` when it
    has none; only ``prepare`` calls it, once per run unless an attempt dies before its
    checkpoint. A context serves one attempt, in one thread.
    """

    model: ChatModel
    limits: Limits
    approved_profile: Callable[[], ApprovedProfile | None]


agent = Agent(WRITING_RUN_KIND, state=WriterState, context=WriterContext)


PostText = Annotated[str, StringConstraints(min_length=1, max_length=rules.MAX_TEXT_CHARS)]
RuleName = Annotated[str, StringConstraints(min_length=1, max_length=40)]
Finding = Annotated[str, StringConstraints(max_length=rules.MAX_FINDING_CHARS)]
FlaggedNumber = Annotated[str, StringConstraints(max_length=rules.MAX_NUMBER_CHARS)]
EvidenceFile = Annotated[
    str, StringConstraints(min_length=1, max_length=rules.MAX_EVIDENCE_FILE_CHARS)
]
EvidenceLines = Annotated[str, StringConstraints(max_length=24)]
ClaimStatement = Annotated[str, StringConstraints(min_length=1, max_length=rules.MAX_CLAIM_CHARS)]
PlanChange = Annotated[str, StringConstraints(max_length=rules.MAX_NOTE_CHARS)]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuleFinding(_Payload):
    rule: RuleName
    finding: Finding


class CheckReport(_Payload):
    """A variant's code-rule report (``rules.check_variant``)."""

    passed: bool
    failures: list[RuleFinding] = Field(max_length=rules.MAX_FINDINGS)
    length: Annotated[int, Field(ge=0)]
    flagged_numbers: list[FlaggedNumber] = Field(max_length=rules.MAX_FLAGGED_NUMBERS)


class ClaimEvidence(_Payload):
    """Where in the repository the feature a claim rests on was found."""

    feature_id: FeatureId
    file: EvidenceFile
    lines: EvidenceLines | None = None


class EvidencedClaim(_Payload):
    statement: ClaimStatement
    feature_ids: list[FeatureId] = Field(max_length=rules.MAX_CLAIM_FEATURES)
    evidence: list[ClaimEvidence] = Field(max_length=rules.MAX_CLAIM_EVIDENCE)


class Alternative(_Payload):
    """Another variant of the round, for the person to read, with what the checks found."""

    index: Annotated[int, Field(ge=0)]
    variant: Variant
    check: CheckReport
    review: VariantReview | None


@agent.action(
    STORE_DRAFT,
    timeout=WRITE_TIMEOUT_SECONDS,
    attempts=WRITE_ATTEMPTS,
    retry_after=WRITE_RETRY_AFTER_SECONDS,
    # Storing a draft of the run's own changes nothing beyond the run: nobody sees it before
    # a person reviews the proposal.
    effect=ActionEffect.INTERNAL,
)
class StoreDraft(_Payload):
    """Store the draft of this run as its content item, ``proposed``; answers its id.

    The fields are what ``db/content_items.NewContentItem`` stores, the cost as a number.
    """

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    text: PostText
    plan: Plan
    plan_changes: list[PlanChange] = Field(max_length=rules.MAX_PLAN_CHANGES)
    alternatives: list[Alternative] = Field(max_length=rules.MAX_SUBMITTED_VARIANTS)
    claims: list[EvidencedClaim] = Field(max_length=rules.MAX_CLAIMS)
    check_report: CheckReport
    critic_report: VariantReview
    flagged_numbers: list[FlaggedNumber] = Field(max_length=rules.MAX_FLAGGED_NUMBERS)
    cost_usd: Annotated[float, Field(ge=0)]


@agent.action(
    PUBLISH_POST,
    timeout=WRITE_TIMEOUT_SECONDS,
    attempts=WRITE_ATTEMPTS,
    retry_after=WRITE_RETRY_AFTER_SECONDS,
    # Publishing puts the post in front of other people: the gate holds it for a person.
    effect=ActionEffect.PUBLISHES,
)
class PublishPost(_Payload):
    """Publish the post of this run: its content item, with the text the person approves."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    content_item_id: uuid.UUID
    text: PostText


def initial_state(run: WritingRunInput, brief: WritingBrief) -> WriterState:
    """What a writing run starts from: the run, its brief, and nothing done yet."""
    return {
        "workspace_id": str(run.workspace_id),
        "project_id": str(run.project_id),
        "run_id": str(run.run_id),
        "brief": brief.model_dump(mode="json"),
        "profile_version_id": "",
        "writing_context": {},
        "plan": {},
        "plan_changes": [],
        "round": 0,
        "variants": [],
        "checks": [],
        "critique": {},
        "earlier_rounds": [],
        "decision": "",
        "reason": "",
        "chosen": -1,
        "passed_critic": False,
        "draft": {},
        "content_item_id": "",
        "status": "",
        "action_error": "",
        "result": {},
        "notes": [],
        "cost_usd": 0.0,
        "model": "",
    }


def result_of(final_state: Mapping[str, Any]) -> WritingResult:
    """What a run whose graph finished produced, as ``record`` left it."""
    result = dict(final_state["result"])
    result["status"] = PostStatus(result["status"])
    return WritingResult(**result)
