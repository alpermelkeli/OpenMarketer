"""What the studio's tests put in place of the world: the models, the image provider, the
sandbox and object storage, all on this machine, and the runs they set up in the database.

The image provider and the sandbox make small PNGs and remember what they were asked; object
storage is a dictionary. The models answer per role from a script, as core's tests of the
graph do. Nothing reaches a network.
"""

import io
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from PIL import Image
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.analysis_runs import mark_run_started as mark_analysis_started
from openmarketer_core.db.analysis_runs import request_analysis_run
from openmarketer_core.db.chat import create_chat
from openmarketer_core.db.models import Project, Workspace
from openmarketer_core.db.repository_images import (
    NewRepositoryImage,
    keep_repository_images,
    list_repository_images,
    repository_image_key,
)
from openmarketer_core.db.session import transaction
from openmarketer_core.db.studio_runs import mark_run_started, request_studio_run
from openmarketer_core.llm import ChatReply
from openmarketer_core.repository_analysis.repository_images.selection import (
    RepositoryImageKind,
)
from openmarketer_core.studio.asset_store import AssetStoreUnavailable
from openmarketer_core.studio.media_provider import ImageInput, MediaResult
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.request import REQUESTED_BY_A_PERSON, REQUESTED_BY_THE_CHAT
from openmarketer_core.studio.requests import AssetRequest
from openmarketer_core.studio.sandbox import (
    CanvasSize,
    ImageMime,
    SandboxFile,
    SandboxLogs,
    SandboxResult,
)
from openmarketer_core.studio.studio_agent import rules
from openmarketer_core.studio.workflow_contract import StudioRunInput
from openmarketer_worker.creative_studio.policy import StudioPolicy

# Short limits: a test that waits for a second attempt waits a second.
QUICK = StudioPolicy(
    attempt_timeout_seconds=60, heartbeat_timeout_seconds=10, max_attempts=2, retry_after_seconds=1
)
# The creative budget of the tests' runs: ten dollars a month, a quarter on its own.
CAPS = {"MEDIA_MONTHLY_CAP_USD": "10", "MEDIA_AUTO_APPROVE_USD": "0.25"}
GOAL = "Announce the spring launch of Acme Notes."
STYLE_NOTES = "Warm morning light, a quiet desk."
COPY = ["Launch day"]
IMAGE_COST = Decimal("0.05")
ESTIMATE = Decimal("0.08")


def a_png(width: int = 1080, height: int = 1080, colour=(240, 200, 80)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, format="PNG")
    return out.getvalue()


def size_of(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.size


# ---------------------------------------------------------------------- object storage
@dataclass
class MemoryAssets:
    """``AssetStore`` in a dictionary; ``unavailable`` makes every call fail as a service would."""

    objects: dict[str, bytes] = field(default_factory=dict)
    unavailable: bool = False

    def put(self, key: str, body: bytes, *, content_type: ImageMime) -> None:
        self._check()
        self.objects[key] = body

    def get(self, key: str) -> bytes:
        self._check()
        return self.objects[key]

    def remove(self, key: str) -> None:
        self._check()
        self.objects.pop(key, None)

    def _check(self) -> None:
        if self.unavailable:
            raise AssetStoreUnavailable("the object storage could not be reached")


# ---------------------------------------------------------------------- the image provider
@dataclass
class FakeMedia:
    """A ``MediaProvider`` that draws plain PNGs at ``size`` and remembers each call.

    ``failures`` are raised one for each call, before anything is drawn.
    """

    size: tuple[int, int] = (1080, 1080)
    cost_usd: Decimal = IMAGE_COST
    failures: list[Exception] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate(
        self, prompt: str, *, width: int, height: int, references: Sequence[ImageInput] = ()
    ) -> MediaResult:
        return self._draw("generate", prompt, width, height, None, references)

    def edit(
        self,
        image: ImageInput,
        instruction: str,
        *,
        width: int,
        height: int,
        references: Sequence[ImageInput] = (),
    ) -> MediaResult:
        return self._draw("edit", instruction, width, height, image, references)

    def estimate(self, operation) -> Decimal:
        return ESTIMATE

    def _draw(self, operation, text, width, height, source, references) -> MediaResult:
        self.calls.append(
            {
                "operation": operation,
                "text": text,
                "asked": (width, height),
                "source": None if source is None else source.data,
                "references": [reference.data for reference in references],
            }
        )
        if self.failures:
            raise self.failures.pop(0)
        return MediaResult(
            data=a_png(*self.size),
            mime="image/png",
            width=self.size[0],
            height=self.size[1],
            cost_usd=self.cost_usd,
            provider="fake",
            model="fake/image-model",
            params={"operation": operation},
        )


# ---------------------------------------------------------------------- the sandbox
@dataclass
class FakeSandbox:
    """A ``Sandbox`` whose runs leave ``made`` (a PNG at the canvas size by default), or end
    as ``result`` says; ``ocr`` reads ``text``. It remembers the inputs of each run."""

    text: str = "Launch day"
    result: SandboxResult | None = None
    failures: list[Exception] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    read: list[bytes] = field(default_factory=list)

    def run_python(self, code, inputs, requirements, size: CanvasSize) -> SandboxResult:
        return self._run("run_python", dict(inputs), size.width, size.height)

    def render_html(self, html, inputs, width, height) -> SandboxResult:
        return self._run("render_html", dict(inputs), width, height)

    def ocr(self, image: bytes) -> str:
        if self.failures:
            raise self.failures.pop(0)
        self.read.append(image)
        return self.text

    def _run(self, operation, inputs, width, height) -> SandboxResult:
        self.runs.append({"operation": operation, "inputs": inputs})
        if self.failures:
            raise self.failures.pop(0)
        if self.result is not None:
            return self.result
        data = a_png(width, height, (20, 40, 60))
        file = SandboxFile("out.png", "image/png", data, width, height)
        return SandboxResult(files=(file,), logs=SandboxLogs(), duration_s=0.5, exit_code=0)


def a_failed_program(stderr: str) -> SandboxResult:
    return SandboxResult(files=(), logs=SandboxLogs(stderr=stderr), duration_s=0.2, exit_code=1)


# ---------------------------------------------------------------------- the models
Answer = ChatReply | Exception | Callable[[list[dict[str, Any]]], ChatReply]


class ScriptedChat:
    """Prepared answers per role, in order: a reply, an error to raise, or a function of the
    messages. A role whose answers are used up is a defect of the test."""

    def __init__(self, **answers: list[Answer]):
        self.answers = {role: list(queue) for role, queue in answers.items()}
        self.calls: list[str] = []

    def chat(self, role, messages, *, tools=None, tool_choice=None) -> ChatReply:
        self.calls.append(role)
        answer = self.answers[role].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer(messages) if callable(answer) else answer

    def left(self) -> dict[str, int]:
        return {role: len(queue) for role, queue in self.answers.items() if queue}


def text_reply(content: dict[str, Any]) -> ChatReply:
    return ChatReply(
        message={"role": "assistant", "content": json.dumps(content)}, model="m", cost_usd=0.01
    )


def call_reply(name: str, arguments: dict[str, Any]) -> ChatReply:
    tool_call = {
        "id": f"call-{uuid.uuid4().hex[:6]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    return ChatReply(
        message={"role": "assistant", "tool_calls": [tool_call]}, model="m", cost_usd=0.01
    )


def a_brief() -> ChatReply:
    return text_reply(
        {
            "goal": "Announce the launch.",
            "visual_concept": "A bright desk at dawn.",
            "technique": "generated",
            "image_prompt": "A bright desk at dawn, top view.",
            "copy_placement": "Centered, upper third.",
        }
    )


def generating(prompt: str = "A bright desk at dawn, top view.") -> ChatReply:
    return call_reply(rules.SUBMIT_OPERATION, {"op": "generate_image", "prompt": prompt})


def ready() -> Callable[[list[dict[str, Any]]], ChatReply]:
    """The model names the latest image of the run as the round's candidate."""

    def answer(messages: list[dict[str, Any]]) -> ChatReply:
        images = json.loads(messages[1]["content"][0]["text"])["images"]
        latest = images[-1]["version_id"]
        return call_reply(rules.SUBMIT_OPERATION, {"op": "candidate_ready", "version_id": latest})

    return answer


def a_verdict() -> ChatReply:
    return text_reply(
        {
            "criteria": [
                {"criterion": name, "passed": True, "severity": "none", "note": ""}
                for name in rules.CRITERIA
            ],
            "edit_suggestions": [],
        }
    )


def one_round_that_passes() -> ScriptedChat:
    """The models of a run whose generated image passes its first round."""
    return ScriptedChat(
        creative_director=[a_brief()],
        studio=[generating(), ready()],
        asset_verifier=[a_verdict()],
    )


# ---------------------------------------------------------------------- the runs
@dataclass(frozen=True)
class RequestedStudio:
    run: StudioRunInput
    chat_id: uuid.UUID | None


@dataclass(frozen=True)
class StudioDatabase:
    sessions: sessionmaker[Session]

    def requested(
        self,
        *,
        from_a_chat: bool = False,
        exact_copy: Sequence[str] = tuple(COPY),
        reference_asset_ids: Sequence[uuid.UUID] = (),
        project: tuple[uuid.UUID, uuid.UUID] | None = None,
        started: bool = False,
    ) -> RequestedStudio:
        """A queued studio run of a new project in a new workspace (or of ``project``)."""
        with transaction(self.sessions) as session:
            if project is None:
                workspace = Workspace(name="Acme")
                session.add(workspace)
                session.flush()
                record = Project(
                    workspace_id=workspace.id,
                    name="Acme Notes",
                    source_repo_url="https://example.com/acme/notes.git",
                )
                session.add(record)
                session.flush()
                workspace_id, project_id = workspace.id, record.id
            else:
                workspace_id, project_id = project
            chat_id = (
                create_chat(session, workspace_id=workspace_id, project_id=project_id).id
                if from_a_chat
                else None
            )
            run = request_studio_run(
                session,
                workspace_id=workspace_id,
                project_id=project_id,
                request=AssetRequest(
                    goal=GOAL,
                    exact_copy=list(exact_copy),
                    platform=Platform.INSTAGRAM_POST,
                    style_notes=STYLE_NOTES,
                    reference_asset_ids=list(reference_asset_ids),
                ),
                requested_by=REQUESTED_BY_THE_CHAT if from_a_chat else REQUESTED_BY_A_PERSON,
                chat_thread_id=chat_id,
            )
            if started:
                mark_run_started(
                    session, workspace_id=workspace_id, project_id=project_id, run_id=run.id
                )
        return RequestedStudio(
            run=StudioRunInput(workspace_id=workspace_id, project_id=project_id, run_id=run.id),
            chat_id=chat_id,
        )

    def repository_images(
        self, run: StudioRunInput, assets: MemoryAssets, *images: tuple[str, bytes]
    ) -> list[uuid.UUID]:
        """Keep ``images`` (a kind and the bytes of each) as the project's repository images,
        as an analysis of it would, their bytes in ``assets``; their ids, in the given order."""
        scope = {"workspace_id": run.workspace_id, "project_id": run.project_id}
        with transaction(self.sessions) as session:
            analysis = request_analysis_run(session, **scope)
            mark_analysis_started(session, **scope, run_id=analysis.id)
            kept = []
            for n, (kind, data) in enumerate(images):
                sha256 = f"{n:064x}"
                key = repository_image_key(
                    **scope, analysis_run_id=analysis.id, sha256=sha256, mime="image/png"
                )
                assets.objects[key] = data
                kept.append(
                    NewRepositoryImage(
                        path=f"assets/{kind}-{n}.png",
                        kind=RepositoryImageKind(kind),
                        mime="image/png",
                        width=size_of(data)[0],
                        height=size_of(data)[1],
                        size_bytes=len(data),
                        sha256=sha256,
                        storage_key=key,
                    )
                )
            keep_repository_images(session, **scope, analysis_run_id=analysis.id, images=kept)
            listed = list_repository_images(
                session, workspace_id=run.workspace_id, project_id=run.project_id, limit=50
            ).items
        by_path = {image.path: image.id for image in listed}
        return [by_path[image.path] for image in kept]
