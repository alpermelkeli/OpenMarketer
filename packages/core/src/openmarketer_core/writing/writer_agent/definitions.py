"""The writer's declarations: the agent, its state, what its nodes are given, and its action.

This is the module the worker's workflow loads, so it imports no graph framework and nothing
that touches the database. The nodes are in ``graph.py``, which only the process that runs the
graph imports; importing it is what puts them on ``agent``.

- ``WriterState`` is what the graph keeps between steps and what a checkpoint stores: plain
  data only. ``prepare`` copies what the run writes from (the writing context) out of the
  approved profile into it, with the version's id, so every later step and every later attempt
  writes from that version, whatever a person approves meanwhile.
- ``WriterContext`` is what a node needs that is not state: the model of the attempt, which
  answers the roles ``writer`` and ``critic``, the limits, and three readings ``prepare`` asks
  for: the project's approved profile, and the asset and the research run a brief may name.
- The two actions. ``StoreDraft`` is internal: it stores the draft the person will review as
  the run's content item, ``proposed``, and answers ``{"content_item_id"}``; storing the same
  draft again gives back the stored item, so repeating it is harmless (keyed by the action's
  id). Its payload carries the draft, model-written text included, as the studio's operations
  carry a prompt: every field is bounded, and the whole weighs ``rules.MAX_DRAFT_BYTES`` at
  most, because every payload enters the workflow's history. For that reason too it carries no
  evidence of a claim, which names files of the repository: each claim names the features it
  rests on, and the handler stores the evidence of those features in the profile version the
  run recorded. It names the brief's asset, the version of it the post was written for and the
  research run, when the brief named them.
  ``PublishPost`` publishes, so the policy gate holds it as a proposal until a person approves
  or rejects it; its payload names the content item and carries the text and the image's
  version, so what the person approves is in the proposal itself. In the first step of the
  writer its handler records the post as it would have been sent (``dry_run``) and sends
  nothing; repeating it is harmless, keyed by the action's id.

The ids of the inputs are left out of a payload that has none (``_absent``), so a thread that
asked for an action before they existed asks for the same one when its node runs again.
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
    PostImage,
    PostStatus,
    ResearchFindings,
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
    has none. ``post_image`` reads an asset of the project's Studio library as the writer reads
    it (never its image), and raises ``rules.PostImageNotUsable`` when the project has no such
    asset or a post cannot go with it; ``research_findings`` reads a succeeded research run of
    the project with the first ``rules.MAX_POST_FINDINGS`` findings of its report, each source
    with ``rules.MAX_SITE_QUOTES`` quotes, and raises ``rules.ResearchNotUsable`` when the
    project has no such run or it has not succeeded. Only ``prepare`` calls them, the last two
    only for a brief that names one, once per run unless an attempt dies before its checkpoint.
    A context serves one attempt, in one thread.
    """

    model: ChatModel
    limits: Limits
    approved_profile: Callable[[], ApprovedProfile | None]
    post_image: Callable[[uuid.UUID], PostImage]
    research_findings: Callable[[uuid.UUID], ResearchFindings]


agent = Agent(WRITING_RUN_KIND, state=WriterState, context=WriterContext)


PostText = Annotated[str, StringConstraints(min_length=1, max_length=rules.MAX_TEXT_CHARS)]
RuleName = Annotated[str, StringConstraints(min_length=1, max_length=40)]
Finding = Annotated[str, StringConstraints(max_length=rules.MAX_FINDING_CHARS)]
FlaggedNumber = Annotated[str, StringConstraints(max_length=rules.MAX_NUMBER_CHARS)]
ClaimStatement = Annotated[str, StringConstraints(min_length=1, max_length=rules.MAX_CLAIM_CHARS)]
PlanChange = Annotated[str, StringConstraints(max_length=rules.MAX_NOTE_CHARS)]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _absent(value: uuid.UUID | None) -> bool:
    return value is None


# The id of a ready input of the brief, left out of the payload when the brief named none.
InputId = Annotated[uuid.UUID | None, Field(exclude_if=_absent)]


class RuleFinding(_Payload):
    rule: RuleName
    finding: Finding


class CheckReport(_Payload):
    """A variant's code-rule report (``rules.check_variant``)."""

    passed: bool
    failures: list[RuleFinding] = Field(max_length=rules.MAX_FINDINGS)
    length: Annotated[int, Field(ge=0)]
    flagged_numbers: list[FlaggedNumber] = Field(max_length=rules.MAX_FLAGGED_NUMBERS)


class DraftClaim(_Payload):
    """A claim of the post and the features it rests on, without their evidence.

    The evidence names files of the repository, which stay out of the workflow's history:
    ``store_draft``'s handler adds it from the profile version the run recorded.
    """

    statement: ClaimStatement
    feature_ids: list[FeatureId] = Field(max_length=rules.MAX_CLAIM_FEATURES)


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

    The fields are what ``db/content_items.NewContentItem`` stores, the cost as a number, but
    for the claims' evidence, which the handler adds (``DraftClaim``).
    """

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    text: PostText
    plan: Plan
    plan_changes: list[PlanChange] = Field(max_length=rules.MAX_PLAN_CHANGES)
    alternatives: list[Alternative] = Field(max_length=rules.MAX_SUBMITTED_VARIANTS)
    claims: list[DraftClaim] = Field(max_length=rules.MAX_CLAIMS)
    check_report: CheckReport
    critic_report: VariantReview
    flagged_numbers: list[FlaggedNumber] = Field(max_length=rules.MAX_FLAGGED_NUMBERS)
    cost_usd: Annotated[float, Field(ge=0)]
    asset_id: InputId = None
    asset_version_id: InputId = None
    research_run_id: InputId = None


@agent.action(
    PUBLISH_POST,
    timeout=WRITE_TIMEOUT_SECONDS,
    attempts=WRITE_ATTEMPTS,
    retry_after=WRITE_RETRY_AFTER_SECONDS,
    # Publishing puts the post in front of other people: the gate holds it for a person.
    effect=ActionEffect.PUBLISHES,
)
class PublishPost(_Payload):
    """Publish the post of this run: its content item, with the text and the version of the
    image the person approves (none for a post without one)."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    content_item_id: uuid.UUID
    text: PostText
    asset_version_id: InputId = None


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
