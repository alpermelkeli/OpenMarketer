"""The creative studio's declarations: the agent, its state, what its nodes are given, its actions.

This is the module the worker's workflow loads, so it imports no graph framework and nothing
that touches the database. The nodes are in ``graph.py``, which only the process that runs the
graph imports; importing it is what puts them on ``agent``.

- ``StudioState`` is what the graph keeps between steps and what a checkpoint stores: plain
  data only, and never an image. An image is stored by the handler of the action that made it,
  as a version of the run's asset, and the graph carries its id and its size; a node that
  shows one to a model reads it through its context (``image_of``).
- ``StudioContext`` is what a node needs that is not state: the model of the attempt, the
  limits, the stored image of a version of the run, the brand kit, and the product's images
  an analysis kept from its repository (their list, and the bytes of one by its id). The
  brief keeps the list in the state, ids, paths, kinds and sizes only, so that every later
  step of the run names the same images.
- The actions: ``generate_image`` and ``edit_image`` spend (the image provider is paid), and
  the gate weighs them against the creative budget; ``render_html``, ``run_python`` and
  ``read_text`` run in the installation's sandbox and are internal. Each of the four that make
  an image stores it as the version its payload names, and answers with
  ``{"version_id", "asset_id", "width", "height", "mime", "size_bytes", "cost_usd"}``;
  ``read_text`` answers with ``{"text"}``, what OCR read in a version. A payload names
  identifiers and the short text a model wrote (a prompt, a page, a program), each bounded;
  no field is read as a path, and the handler decides what an id may name.
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from openmarketer_core.agent_runtime.actions import ActionEffect
from openmarketer_core.agent_runtime.agent import Agent
from openmarketer_core.llm import ChatModel
from openmarketer_core.studio.brand_kit import BrandKit
from openmarketer_core.studio.media_provider import ImageInput
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.studio.studio_agent import rules
from openmarketer_core.studio.studio_agent.rules import (
    Limits,
    RepositoryImageInfo,
    StudioResult,
    Technique,
    VersionReview,
)
from openmarketer_core.studio.workflow_contract import STUDIO_RUN_KIND, StudioRunInput

GENERATE_IMAGE = rules.GENERATE_IMAGE
EDIT_IMAGE = rules.EDIT_IMAGE
RENDER_HTML = rules.RENDER_HTML
RUN_PYTHON = rules.RUN_PYTHON
READ_TEXT = "read_text"

# How the actions are tried. Constants of the actions and not settings.
# - A generation or an edit is one call to the image provider, which takes up to a couple of
#   minutes. It is paid for, so it is tried once: a second attempt would pay again, and the
#   handler stores one version per action id whatever happens.
# - A render and a program run in the sandbox: repeating one costs nothing and stores the one
#   version of its action id, so each is tried twice, a program with the time an install of
#   its packages takes.
# - Reading text is a short OCR run, tried three times.
# - A minute without a heartbeat is a worker that is gone.
IMAGE_TIMEOUT_SECONDS = 240
RENDER_TIMEOUT_SECONDS = 120
RENDER_ATTEMPTS = 2
PYTHON_TIMEOUT_SECONDS = 360
PYTHON_ATTEMPTS = 2
SANDBOX_RETRY_AFTER_SECONDS = 10
READ_TEXT_TIMEOUT_SECONDS = 120
READ_TEXT_ATTEMPTS = 3
READ_TEXT_RETRY_AFTER_SECONDS = 5
HEARTBEAT_SECONDS = 30


class StudioState(TypedDict):
    # The run, as text because a checkpoint stores plain data.
    workspace_id: str
    project_id: str
    run_id: str
    # The request, as it was stored with the run.
    goal: str
    exact_copy: list[str]
    platform: str
    style_notes: str
    reference_asset_ids: list[str]
    # What the graph has done.
    brief: dict[str, Any]  # the creative director's ``Brief``; {} until it is made
    # The product's repository images, as the brief was shown them
    # (``rules.repository_images_data``): never their bytes. A thread stored before this key
    # was kept has none, and its run goes on without them.
    repository_images: list[dict[str, Any]]
    # What every generation and edit is told of the product (``rules.about_product``), taken
    # from the brand kit with the brief so that a later attempt asks for the same prompt.
    about_product: str
    technique: str  # a ``Technique``'s value: the one this round uses
    round: int  # 0 until the brief, then the round being made
    # This round's operations as they ended: {"op", "version_id"?, "ok", "error"?, "note"?}.
    operations: list[dict[str, Any]]
    # The operation ``operate`` asks for next, as produce made it ({} when none waits). Its
    # ``version_id`` is made with it, so that the same request is asked again on a retry.
    pending: dict[str, Any]
    asset_id: str  # the run's asset, "" until its first version is stored
    # Every image the run made: {"version_id", "operation", "technique", "round", "width",
    # "height", "mime", "size_bytes", "cost_usd"}.
    versions: list[dict[str, Any]]
    candidate: str  # this round's candidate version, "" until one is chosen
    # Every candidate with its reviews: {"version_id", "round", "technique", "check",
    # "verifier", "passed", "failures"}.
    candidates: list[dict[str, Any]]
    check: dict[str, Any]  # this round's check report; {} until it is made
    verification: dict[str, Any]  # this round's verifier report; {} when it was not asked
    edit_plan: dict[str, Any]  # the plan of this round; {} in the first round
    failures: list[dict[str, Any]]  # {"round", "technique", "failures"}, one per failed round
    decision: str  # what the last decide chose: "finalize", "edit" or "escalate"
    reason: str  # the code's sentence of why it chose it
    result: dict[str, Any]  # {"final_version_id", "needs_review", "reason"} once ended
    notes: list[str]  # what the code dropped or worked around
    cost_usd: float  # the model calls and the paid operations, together
    model: str


@dataclass
class StudioContext:
    """What the nodes of one attempt are given besides the state.

    ``image_of`` gives the stored image of a version of this run, by its id, from object
    storage; it raises when the image cannot be read, which fails the step and lets it be
    tried again. ``brand_kit`` is taken from the project's approved profile for the attempt.
    ``repository_images`` are the project's images of its latest analysis that kept any, read
    for the attempt, and ``repository_image_of`` gives one of them by its id, or ``None`` when
    it is no longer the project's (a later analysis replaced it); it raises as ``image_of``
    does when object storage cannot be read. A context serves one attempt, in one thread.
    """

    model: ChatModel
    limits: Limits
    image_of: Callable[[str], ImageInput]
    brand_kit: BrandKit
    repository_images: Sequence[RepositoryImageInfo] = ()
    repository_image_of: Callable[[str], ImageInput | None] = field(default=lambda image_id: None)


agent = Agent(STUDIO_RUN_KIND, state=StudioState, context=StudioContext)

VersionIds = Annotated[list[uuid.UUID], Field(max_length=rules.MAX_REFERENCES)]
Prompt = Annotated[
    str,
    StringConstraints(min_length=1, max_length=rules.MAX_COMPOSED_PROMPT_CHARS),
]


class _ImagePayload(BaseModel):
    """What every action that makes an image names: the run, the version it stores, its round."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    # The version the handler stores the image as, and the run's asset it belongs to (None
    # for the run's first version, which creates the asset).
    version_id: uuid.UUID
    asset_id: uuid.UUID | None
    round: Annotated[int, Field(ge=1)]
    technique: Technique


@agent.action(
    GENERATE_IMAGE,
    timeout=IMAGE_TIMEOUT_SECONDS,
    heartbeat=HEARTBEAT_SECONDS,
    effect=ActionEffect.SPENDS,
)
class GenerateImage(_ImagePayload):
    """Make an image at the run's platform size from a prompt, following reference images.

    ``prompt`` holds the exact copy as code put it (``rules.generation_prompt``), and a line of
    what each reference is for. References are versions of this run, assets of the project
    the request named and the project's repository images, sent in that order.
    """

    prompt: Prompt
    reference_version_ids: VersionIds = Field(default_factory=list)
    reference_asset_ids: VersionIds = Field(default_factory=list)
    reference_repository_image_ids: VersionIds = Field(default_factory=list)


@agent.action(
    EDIT_IMAGE,
    timeout=IMAGE_TIMEOUT_SECONDS,
    heartbeat=HEARTBEAT_SECONDS,
    effect=ActionEffect.SPENDS,
)
class EditImage(_ImagePayload):
    """Change the version ``source_version_id`` of this run as the instruction says.

    It is sent first, then the references in the order ``GenerateImage`` sends them.
    """

    source_version_id: uuid.UUID
    instruction: Prompt
    reference_version_ids: VersionIds = Field(default_factory=list)
    reference_asset_ids: VersionIds = Field(default_factory=list)
    reference_repository_image_ids: VersionIds = Field(default_factory=list)


@agent.action(
    RENDER_HTML,
    timeout=RENDER_TIMEOUT_SECONDS,
    attempts=RENDER_ATTEMPTS,
    retry_after=SANDBOX_RETRY_AFTER_SECONDS,
    heartbeat=HEARTBEAT_SECONDS,
    effect=ActionEffect.INTERNAL,
)
class RenderHtml(_ImagePayload):
    """Render a page to a PNG at the run's platform size, in the sandbox's browser.

    The images ``input_version_ids`` and ``input_repository_image_ids`` name are beside the
    page as ``<id>.png``.
    """

    html: Annotated[str, StringConstraints(min_length=1, max_length=rules.MAX_HTML_CHARS)]
    input_version_ids: VersionIds = Field(default_factory=list)
    input_repository_image_ids: VersionIds = Field(default_factory=list)


@agent.action(
    RUN_PYTHON,
    timeout=PYTHON_TIMEOUT_SECONDS,
    attempts=PYTHON_ATTEMPTS,
    retry_after=SANDBOX_RETRY_AFTER_SECONDS,
    heartbeat=HEARTBEAT_SECONDS,
    effect=ActionEffect.INTERNAL,
)
class RunPython(_ImagePayload):
    """Run a program the studio's model wrote in the sandbox, and keep the image it leaves.

    The images ``input_version_ids`` and ``input_repository_image_ids`` name are in ``/in`` as
    ``<id>.png``; the handler screens the code and the packages before anything runs
    (``code_screening.py``). A program that fails in its own terms is a failed outcome whose
    error says why.
    """

    code: Annotated[str, StringConstraints(min_length=1, max_length=rules.MAX_CODE_CHARS)]
    requirements: Annotated[
        list[Annotated[str, StringConstraints(max_length=100)]],
        Field(max_length=rules.MAX_PACKAGES),
    ] = Field(default_factory=list)
    input_version_ids: VersionIds = Field(default_factory=list)
    input_repository_image_ids: VersionIds = Field(default_factory=list)


@agent.action(
    READ_TEXT,
    timeout=READ_TEXT_TIMEOUT_SECONDS,
    attempts=READ_TEXT_ATTEMPTS,
    retry_after=READ_TEXT_RETRY_AFTER_SECONDS,
    effect=ActionEffect.INTERNAL,
)
class ReadText(BaseModel):
    """Read the text in the version ``version_id`` of this run with OCR; answers ``{"text"}``."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    version_id: uuid.UUID


def initial_state(run: StudioRunInput, request: AssetRequest) -> StudioState:
    """What a studio run starts from: the run, its request, and nothing done yet."""
    return {
        "workspace_id": str(run.workspace_id),
        "project_id": str(run.project_id),
        "run_id": str(run.run_id),
        "goal": request.goal,
        "exact_copy": list(request.exact_copy),
        "platform": request.platform.value,
        "style_notes": request.style_notes,
        "reference_asset_ids": [str(asset) for asset in request.reference_asset_ids],
        "brief": {},
        "repository_images": [],
        "about_product": "",
        "technique": Technique.GENERATED.value,
        "round": 0,
        "operations": [],
        "pending": {},
        "asset_id": "",
        "versions": [],
        "candidate": "",
        "candidates": [],
        "check": {},
        "verification": {},
        "edit_plan": {},
        "failures": [],
        "decision": "",
        "reason": "",
        "result": {},
        "notes": [],
        "cost_usd": 0.0,
        "model": "",
    }


def result_of(final_state: Mapping[str, Any]) -> StudioResult:
    """The result of a run whose graph finished: the version it ends with, and every review."""
    result = final_state["result"]
    return StudioResult(
        final_version_id=result["final_version_id"],
        needs_review=result["needs_review"],
        reason=result["reason"],
        rounds=final_state["round"],
        cost_usd=final_state["cost_usd"],
        model=final_state["model"],
        notes=list(final_state["notes"]),
        reviews=[
            VersionReview(
                version_id=candidate["version_id"],
                check_report=candidate["check"],
                verifier_report=candidate["verifier"],
                passed=candidate["passed"],
            )
            for candidate in final_state["candidates"]
        ],
    )
