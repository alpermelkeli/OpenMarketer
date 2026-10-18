"""What the runtime's activities do for a studio run: the worker's side of the studio's graph.

The studio's graph and its rules are core's. ``StudioWork`` is what stands between them and
this worker's database, object storage, image provider and sandbox, one method for each thing
the agent runtime asks an agent for:

- ``initial_state``: what a run starts from, which is the run's own request;
- ``context``: what the nodes of one attempt are given: a chat model of the attempt's own,
  the run's limits (with what one paid image is reckoned to cost under the workspace's
  models), the stored image of a version of the run, and the brand kit of the project's
  approved profile;
- ``record_progress``: storing the events of each node as it ends, for the chat that asked
  for the run (``progress.py`` in core says which);
- the handlers of the five actions: ``generate_image`` and ``edit_image`` (the image
  provider, paid), ``render_html`` and ``run_python`` (the sandbox), each storing the image
  it made as the version its payload names, and ``read_text`` (the sandbox's OCR);
- ``estimate``: what a paid image is reckoned to cost, which the creative budget weighs before
  the handler may run;
- ``finish``: storing the reviews of the run's candidates and its outcome, both or neither.

``WordedSteps`` wraps the steps of the graph so that what a studio run fails with leaves the
activity as a failure with a type and a message that may be stored and shown.

They hold no rule of their own: what an operation may name, what a candidate must pass and how
a run changes state are core's. What they add is what an activity owes the engine. A failure
says whether another attempt is worth its cost, and none carries a message written for a log.
Everything can run twice without doing its work twice: a version is stored under the id its
payload names and keyed by the action's id (``add_version``), and a handler that finds the
version of its action stored answers with it without asking the provider or the sandbox again,
so a paid image is paid for once. An operation that did not make its image (the provider
refused, the program failed, a reference is not the run's) fails for good with a short reason
the studio's model reads and may act on; one whose service could not be reached may pass.

No credential is kept here or put in the graph's state: the chat model and the image provider
of an attempt are asked for with the run's workspace (``new_model``, ``new_media``) each time
one is needed. A key that is set nowhere, or a credential that is stored and cannot be read
(``WORKSPACE_FAILURES``), ends the run in the words of its error, which name what to set and
hold no value. An image is never in the graph's state or the workflow history: the handler
stores its bytes in object storage and its row in the database, and answers with its id and
its size; a node that shows one to a model reads it back through the context.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.agent_runtime.actions import ActionOutcome
from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
from openmarketer_core.credentials.resolution import CredentialUnreadable, ModelKeyNotSet
from openmarketer_core.db.media_assets import (
    MediaAssetNotFound,
    MediaAssetVersion,
    MediaAssetVersionNotFound,
    MediaAssetVersionReviewed,
    NewAssetVersion,
    add_version,
    asset_version,
    media_asset,
    record_version_review,
)
from openmarketer_core.db.models import AssetTechnique, StudioRunStatus
from openmarketer_core.db.profile_versions import latest_approved_profile
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.run_events import append_events
from openmarketer_core.db.session import DatabaseError, transaction
from openmarketer_core.db.studio_runs import (
    FinalVersionNotFound,
    StudioRunFinished,
    StudioRunNotFound,
    StudioRunNotStarted,
    StudioRunToExecute,
    mark_run_needs_review,
    mark_run_succeeded,
    run_thread_id,
    run_to_execute,
    studio_run,
)
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import ChatModel, LLMError
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.run_events import RunEvent
from openmarketer_core.studio.asset_store import AssetStore, AssetStoreUnavailable, asset_key
from openmarketer_core.studio.brand_kit import brand_kit_of
from openmarketer_core.studio.failures import STUDIO_FAILURES
from openmarketer_core.studio.media_provider import (
    ImageInput,
    MediaProvider,
    MediaProviderError,
    MediaRefused,
    MediaUnavailable,
)
from openmarketer_core.studio.openrouter_images import fit_to_size
from openmarketer_core.studio.platforms import PlatformSpec, platform_spec
from openmarketer_core.studio.requests import StoredAssetRequestInvalid
from openmarketer_core.studio.sandbox import (
    CanvasSize,
    ImageMime,
    Sandbox,
    SandboxError,
    SandboxFile,
    SandboxResult,
    SandboxUnavailable,
)
from openmarketer_core.studio.studio_agent import rules
from openmarketer_core.studio.studio_agent.definitions import (
    EditImage,
    GenerateImage,
    ReadText,
    RenderHtml,
    RunPython,
    StudioContext,
    StudioState,
    initial_state,
    result_of,
)
from openmarketer_core.studio.workflow_contract import STUDIO_RUN_KIND, StudioRunInput
from openmarketer_core.workspace_settings.model_settings import StoredModelChoiceInvalid
from openmarketer_worker.agent_runtime.activities import AgentGraph
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess, forget_quietly
from openmarketer_worker.agent_runtime.routes import ActionContext
from openmarketer_worker.agent_runtime.vocabulary import DATABASE_UNAVAILABLE, MAX_ERROR_CHARACTERS
from openmarketer_worker.creative_studio import steps
from openmarketer_worker.creative_studio.activities import failure, subject_of

logger = logging.getLogger(__name__)

# What the credentials and settings of a run's workspace are refused with: the model
# provider's key is set nowhere, a stored value does not open, or a stored model choice breaks
# a rule. None passes by itself, and each is worded for whoever asked for the run.
WORKSPACE_FAILURES = (ModelKeyNotSet, CredentialUnreadable, StoredModelChoiceInvalid)
# The most of the text OCR read in an image that the graph is answered with: the copy of a
# request is a few short lines, and an answer is held to the history's bound.
MAX_READ_TEXT_CHARS = 4000
# The most of what a failed program printed on its error stream that its reason repeats.
MAX_PRINTED_CHARS = 300
# What a version made in the sandbox names as its provider.
SANDBOX_PROVIDER = "sandbox"


@dataclass(frozen=True)
class _Image:
    """An image an operation made, before it is stored as a version."""

    data: bytes = field(repr=False)
    mime: ImageMime
    width: int
    height: int
    cost_usd: Decimal
    provider: str | None
    model: str | None
    params: Mapping[str, Any]
    prompt: str | None = None
    code: str | None = None


class StudioWork:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        checkpoints: CheckpointAccess,
        *,
        new_model: Callable[[uuid.UUID], ChatModel],
        new_media: Callable[[uuid.UUID], MediaProvider],
        image_estimate: Callable[[uuid.UUID], Decimal],
        limits: rules.Limits,
        sandbox: Sandbox,
        assets: AssetStore,
    ) -> None:
        """``new_model`` and ``new_media`` make the chat model and the image provider of one
        attempt for a workspace, with the key it has now; either may raise one of
        ``WORKSPACE_FAILURES``. ``image_estimate`` is what one paid image is reckoned to cost
        under the workspace's models, and needs no key. ``limits`` are the run's, as the
        operator set them; ``sandbox`` runs agent-written code and reads text; ``assets``
        keeps the images.
        """
        self._sessions = sessions
        self._checkpoints = checkpoints
        self._new_model = new_model
        self._new_media = new_media
        self._image_estimate = image_estimate
        self._limits = limits
        self._sandbox = sandbox
        self._assets = assets

    # ------------------------------------------------------------------ the graph's needs
    def initial_state(self, run: GraphRunRef) -> StudioState:
        """What the graph of ``run`` starts from: the run and the request it answers."""
        found = self._run_to_execute(run)
        return initial_state(
            StudioRunInput(
                workspace_id=found.workspace_id, project_id=found.project_id, run_id=found.run_id
            ),
            found.request,
        )

    def context(self, run: GraphRunRef, checkpoints: RunCheckpoints) -> StudioContext:
        """What the nodes of one attempt at a step of ``run`` are given."""
        found = self._run_to_execute(run)
        scope = StudioRunInput(
            workspace_id=found.workspace_id, project_id=found.project_id, run_id=found.run_id
        )

        def image_of(version_id: str) -> ImageInput:
            return self._image_for_a_node(scope, version_id)

        with transaction(self._sessions) as session:
            approved = latest_approved_profile(
                session, workspace_id=found.workspace_id, project_id=found.project_id
            )
        return StudioContext(
            model=self._new_model(found.workspace_id),
            limits=self.limits_of(found.workspace_id),
            image_of=image_of,
            brand_kit=brand_kit_of(None if approved is None else approved.profile),
        )

    def limits_of(self, workspace_id: uuid.UUID) -> rules.Limits:
        """The run's limits, with what a paid image costs under the workspace's models."""
        return replace(self._limits, image_estimate_usd=float(self._image_estimate(workspace_id)))

    def record_progress(self, run: GraphRunRef, events: Sequence[RunEvent]) -> None:
        """Store the events of one node of ``run``, for the chat that watches it.

        Its own short transaction, committed as the node ends. The run is handed by its id
        alone, so its workspace and project are read from its row in the same transaction. A
        run that is not there records nothing. Whatever this raises is logged by the step and
        never fails it (``AgentSteps``).
        """
        with transaction(self._sessions) as session:
            found = run_to_execute(session, run.run_id)
            if found is None:
                logger.info("progress of studio run %s not recorded: no such run", run.run_id)
                return
            append_events(
                session,
                workspace_id=found.workspace_id,
                project_id=found.project_id,
                run_kind=STUDIO_RUN_KIND,
                run_id=run.run_id,
                events=events,
            )

    def finish(self, run: GraphRunRef, final_state: Mapping[str, Any]) -> dict[str, Any]:
        """Store the reviews of the run's candidates and its outcome; what the chat is told.

        The run succeeds with the version that passed, or waits for a person with its best
        candidate when a limit was hit (``needs_review``). Safe to repeat: a run that has this
        outcome is given it again without a change.
        """
        finished = StudioRunInput(
            workspace_id=uuid.UUID(final_state["workspace_id"]),
            project_id=uuid.UUID(final_state["project_id"]),
            run_id=uuid.UUID(final_state["run_id"]),
        )
        result = result_of(final_state)
        final_version = uuid.UUID(result.final_version_id)
        try:
            with transaction(self._sessions) as session:
                for review in _last_review_of_each_version(result.reviews):
                    self._record_review(session, finished, review)
                mark = mark_run_needs_review if result.needs_review else mark_run_succeeded
                ended = mark(
                    session,
                    workspace_id=finished.workspace_id,
                    project_id=finished.project_id,
                    run_id=finished.run_id,
                    final_version_id=final_version,
                    cost_usd=Decimal(str(result.cost_usd)),
                )
        except StudioRunFinished as e:
            self._forget_a_failed_run(finished, e)
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except (StudioRunNotFound, StudioRunNotStarted, ProjectNotFound) as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except FinalVersionNotFound as e:
            raise failure(steps.STUDIO_FAILED, str(e)) from None
        except DatabaseError:
            logger.exception("the outcome of studio run %s was not stored", finished.run_id)
            raise failure(DATABASE_UNAVAILABLE, steps.NOT_STORED) from None
        return {
            "asset_id": str(ended.final_asset_id),
            "version_id": result.final_version_id,
            "needs_review": result.needs_review,
            "rounds": result.rounds,
            "cost_usd": result.cost_usd,
        }

    # -------------------------------------------------------------------- the handlers
    def estimate(self, workspace_id: uuid.UUID) -> Decimal:
        """What one paid image is reckoned to cost under the workspace's models."""
        try:
            return self._image_estimate(workspace_id)
        except (MediaProviderError, StoredModelChoiceInvalid) as e:
            raise failure(steps.OPERATION_FAILED, str(e)) from None

    def generate_image(self, payload: GenerateImage, context: ActionContext) -> Mapping[str, Any]:
        """Have the image provider make the image, and store it as the payload's version."""
        run = _run_of(payload)
        stored = self._stored_answer(run, payload.version_id, context)
        if stored is not None:
            return stored
        spec = self._spec_of(run)
        references = self._references(
            run, payload.reference_version_ids, payload.reference_asset_ids
        )
        made = self._drawn(
            run,
            lambda provider: provider.generate(
                payload.prompt, width=spec.width, height=spec.height, references=references
            ),
            spec,
            prompt=payload.prompt,
        )
        return self._store(payload, context, made)

    def edit_image(self, payload: EditImage, context: ActionContext) -> Mapping[str, Any]:
        """Have the image provider change a version of the run, and store the result."""
        run = _run_of(payload)
        stored = self._stored_answer(run, payload.version_id, context)
        if stored is not None:
            return stored
        spec = self._spec_of(run)
        source = self._image_of_the_run(run, payload.source_version_id)
        references = self._references(
            run, payload.reference_version_ids, payload.reference_asset_ids
        )
        made = self._drawn(
            run,
            lambda provider: provider.edit(
                source,
                payload.instruction,
                width=spec.width,
                height=spec.height,
                references=references,
            ),
            spec,
            prompt=payload.instruction,
        )
        return self._store(payload, context, made)

    def render_html(self, payload: RenderHtml, context: ActionContext) -> Mapping[str, Any]:
        """Render the page in the sandbox at the platform's size, and store the PNG."""
        run = _run_of(payload)
        stored = self._stored_answer(run, payload.version_id, context)
        if stored is not None:
            return stored
        spec = self._spec_of(run)
        inputs = self._sandbox_inputs(run, payload.input_version_ids)
        result = self._in_the_sandbox(
            lambda: self._sandbox.render_html(payload.html, inputs, spec.width, spec.height)
        )
        made = _made_in_the_sandbox(result, spec, "render_html", code=payload.html)
        return self._store(payload, context, made)

    def run_python(self, payload: RunPython, context: ActionContext) -> Mapping[str, Any]:
        """Run the program in the sandbox and store the image it left."""
        run = _run_of(payload)
        stored = self._stored_answer(run, payload.version_id, context)
        if stored is not None:
            return stored
        spec = self._spec_of(run)
        inputs = self._sandbox_inputs(run, payload.input_version_ids)
        result = self._in_the_sandbox(
            lambda: self._sandbox.run_python(
                payload.code, inputs, payload.requirements, CanvasSize(spec.width, spec.height)
            )
        )
        made = _made_in_the_sandbox(
            result, spec, "run_python", code=payload.code, requirements=payload.requirements
        )
        return self._store(payload, context, made)

    def read_text(self, payload: ReadText, context: ActionContext) -> Mapping[str, Any]:
        """The text OCR reads in a version of the run. Reads and changes nothing else."""
        run = StudioRunInput(
            workspace_id=payload.workspace_id, project_id=payload.project_id, run_id=payload.run_id
        )
        image = self._image_of_the_run(run, payload.version_id)
        text = self._in_the_sandbox(lambda: self._sandbox.ocr(image.data))
        return {"text": text[:MAX_READ_TEXT_CHARS]}

    # ---------------------------------------------------------------------- the details
    def _drawn(
        self,
        run: StudioRunInput,
        draw: Callable[[MediaProvider], Any],
        spec: PlatformSpec,
        *,
        prompt: str,
    ) -> _Image:
        """The image the provider drew, fitted to the platform's size."""
        try:
            result = draw(self._new_media(run.workspace_id))
        except MediaRefused as e:
            raise failure(steps.OPERATION_FAILED, str(e)) from None
        except MediaUnavailable as e:
            raise failure(steps.STUDIO_UNAVAILABLE, str(e)) from None
        except (MediaProviderError, ValueError, *WORKSPACE_FAILURES, ConfigError) as e:
            raise failure(steps.OPERATION_FAILED, str(e)) from None
        data, mime, fitted = fit_to_size(result.data, result.mime, spec.width, spec.height)
        params = dict(result.params)
        if fitted != "none":
            params["fitted_by_the_worker"] = fitted
        return _Image(
            data=data,
            mime=mime,
            width=spec.width,
            height=spec.height,
            cost_usd=result.cost_usd,
            provider=result.provider,
            model=result.model,
            params=params,
            prompt=prompt,
        )

    def _in_the_sandbox[T](self, work: Callable[[], T]) -> T:
        try:
            return work()
        except SandboxUnavailable as e:
            raise failure(steps.STUDIO_UNAVAILABLE, str(e)) from None
        except SandboxError as e:
            # Screening's reasons or a fixed sentence: never what the sandbox printed.
            raise failure(steps.OPERATION_FAILED, _cut(str(e))) from None

    def _store(
        self,
        payload: GenerateImage | EditImage | RenderHtml | RunPython,
        context: ActionContext,
        made: _Image,
    ) -> Mapping[str, Any]:
        """Store the image's bytes, then its row; the answer the graph keeps of it."""
        run = _run_of(payload)
        key = asset_key(
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            run_id=run.run_id,
            version_id=payload.version_id,
            mime=made.mime,
        )
        try:
            self._assets.put(key, made.data, content_type=made.mime)
        except AssetStoreUnavailable as e:
            if made.cost_usd > 0:
                # The provider was paid for an image that is lost: the type the creative budget
                # commits the estimate for, so that the month's spending counts it.
                logger.warning("a paid image of studio run %s was not stored: %s", run.run_id, e)
                raise failure(DATABASE_UNAVAILABLE, steps.VERSION_NOT_STORED) from None
            raise failure(steps.STUDIO_UNAVAILABLE, str(e)) from None
        try:
            with transaction(self._sessions) as session:
                stored = add_version(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    run_id=run.run_id,
                    version_id=payload.version_id,
                    action_id=context.action_id,
                    asset_id=payload.asset_id,
                    version=NewAssetVersion(
                        round=payload.round,
                        technique=AssetTechnique(payload.technique.value),
                        storage_key=key,
                        mime=made.mime,
                        width=made.width,
                        height=made.height,
                        size_bytes=len(made.data),
                        cost_usd=made.cost_usd,
                        provider=made.provider,
                        model=made.model,
                        prompt=made.prompt,
                        params=made.params,
                        code=made.code,
                    ),
                )
        except (
            StudioRunNotFound,
            StudioRunNotStarted,
            StudioRunFinished,
            MediaAssetNotFound,
            ProjectNotFound,
        ) as e:
            self._remove_quietly(key)
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except DatabaseError:
            self._remove_quietly(key)
            logger.exception("a version of studio run %s was not stored", run.run_id)
            raise failure(DATABASE_UNAVAILABLE, steps.VERSION_NOT_STORED) from None
        if stored.storage_key != key:
            # An earlier attempt stored the version first, under its own key.
            self._remove_quietly(key)
        return _answer(stored)

    def _stored_answer(
        self, run: StudioRunInput, version_id: uuid.UUID, context: ActionContext
    ) -> Mapping[str, Any] | None:
        """The answer of the version this action stored before, or ``None`` if it stored none."""
        found = self._version(run, version_id)
        if found is None:
            return None
        if found.action_id != context.action_id:
            raise failure(steps.OPERATION_FAILED, f"version {version_id} is another operation's")
        return _answer(found)

    def _version(self, run: StudioRunInput, version_id: uuid.UUID) -> MediaAssetVersion | None:
        """The version ``version_id`` when it is one of the run's own, else ``None``."""
        with transaction(self._sessions) as session:
            try:
                version = asset_version(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    version_id=version_id,
                )
                asset = media_asset(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    asset_id=version.asset_id,
                )
            except (MediaAssetVersionNotFound, MediaAssetNotFound, ProjectNotFound):
                return None
        return version if asset.run_id == run.run_id else None

    def _image_of_the_run(self, run: StudioRunInput, version_id: uuid.UUID) -> ImageInput:
        """The stored image of a version of the run; refused for any other version."""
        version = self._version(run, version_id)
        if version is None:
            raise failure(steps.OPERATION_FAILED, f"{version_id} is not an image of this run")
        return self._bytes_of(version)

    def _image_for_a_node(self, run: StudioRunInput, version_id: str) -> ImageInput:
        """What ``StudioContext.image_of`` gives a node: a failure fails the step."""
        try:
            parsed = uuid.UUID(version_id)
        except ValueError:
            raise failure(steps.STUDIO_FAILED, "an image of the run is named unreadably") from None
        version = self._version(run, parsed)
        if version is None:
            raise failure(steps.STUDIO_FAILED, f"{version_id} is not an image of this run")
        return self._bytes_of(version)

    def _references(
        self,
        run: StudioRunInput,
        version_ids: Sequence[uuid.UUID],
        asset_ids: Sequence[uuid.UUID],
    ) -> list[ImageInput]:
        """The images an operation follows: versions of this run, and the current image of
        each asset of the project it names."""
        images = [self._image_of_the_run(run, version_id) for version_id in version_ids]
        return images + [self._current_image_of(run, asset_id) for asset_id in asset_ids]

    def _current_image_of(self, run: StudioRunInput, asset_id: uuid.UUID) -> ImageInput:
        not_there = failure(
            steps.OPERATION_FAILED, f"{asset_id} is not an asset of this project with an image"
        )
        with transaction(self._sessions) as session:
            try:
                asset = media_asset(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    asset_id=asset_id,
                )
                if asset.current_version_id is None:
                    raise not_there
                version = asset_version(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    version_id=asset.current_version_id,
                )
            except (MediaAssetNotFound, MediaAssetVersionNotFound, ProjectNotFound):
                raise not_there from None
        return self._bytes_of(version)

    def _sandbox_inputs(
        self, run: StudioRunInput, version_ids: Sequence[uuid.UUID]
    ) -> dict[str, bytes]:
        """The versions a program or a page is given, as ``<version_id>.png`` beside it."""
        return {
            f"{version_id}.png": self._image_of_the_run(run, version_id).data
            for version_id in version_ids
        }

    def _bytes_of(self, version: MediaAssetVersion) -> ImageInput:
        try:
            data = self._assets.get(version.storage_key)
        except AssetStoreUnavailable as e:
            raise failure(steps.STUDIO_UNAVAILABLE, str(e)) from None
        return ImageInput(data=data, mime=version.mime)

    def _spec_of(self, run: StudioRunInput) -> PlatformSpec:
        """What the run's platform asks of an image; the run must still be the workspace's."""
        try:
            with transaction(self._sessions) as session:
                found = studio_run(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    run_id=run.run_id,
                )
        except StudioRunNotFound as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        return platform_spec(found.platform)

    def _run_to_execute(self, run: GraphRunRef) -> StudioRunToExecute:
        with transaction(self._sessions) as session:
            found = run_to_execute(session, run.run_id)
        if found is None:
            raise failure(steps.RUN_UNAVAILABLE, f"there is no studio run {run.run_id}")
        return found

    def _record_review(
        self, session: Session, run: StudioRunInput, review: rules.VersionReview
    ) -> None:
        try:
            record_version_review(
                session,
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                version_id=uuid.UUID(review.version_id),
                check_report=review.check_report,
                verifier_report=review.verifier_report,
                passed=review.passed,
            )
        except MediaAssetVersionReviewed as e:
            # The version was reviewed with another outcome in an earlier round; that stands.
            logger.info("a review of studio run %s was kept as it was: %s", run.run_id, e)

    def _forget_a_failed_run(self, run: StudioRunInput, refused: StudioRunFinished) -> None:
        if refused.status is StudioRunStatus.FAILED:
            # Its checkpoints were forgotten when the failure was recorded; this removes what
            # the attempt has written since.
            forget_quietly(
                self._checkpoints, run_thread_id(run.run_id), subject=subject_of(run.run_id)
            )

    def _remove_quietly(self, key: str) -> None:
        """Take back an image whose row was not stored; failing to is logged, not raised."""
        try:
            self._assets.remove(key)
        except AssetStoreUnavailable as e:
            logger.warning("an image whose row was not stored stays in object storage: %s", e)


class WordedSteps:
    """The steps of the studio's graph, with a studio run's failures worded for its run."""

    def __init__(self, graph: AgentGraph) -> None:
        self._graph = graph

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        """Take the step, as ``AgentGraph.advance`` says.

        A brief or an answer that cannot be read, a request that no longer fits its schema,
        a model whose key is not set, a configuration that refuses the call and an image role
        with no estimate end the run (``STUDIO_FAILED``); only the model provider fails in
        ways that pass, an outage or a rate limit (``MODEL_UNAVAILABLE``, tried again). The
        message is the error's own, which names no value.
        """
        try:
            return self._graph.advance(run, checkpoints, resume)
        except (
            *STUDIO_FAILURES,
            StoredAssetRequestInvalid,
            *WORKSPACE_FAILURES,
            ConfigError,
            MediaProviderError,
        ) as e:
            logger.info("studio run %s failed: %s", run.run_id, e)
            passing = isinstance(e, LLMError) and e.may_pass
            kind = steps.MODEL_UNAVAILABLE if passing else steps.STUDIO_FAILED
            raise failure(kind, str(e)) from None


def _run_of(payload: GenerateImage | EditImage | RenderHtml | RunPython) -> StudioRunInput:
    return StudioRunInput(
        workspace_id=payload.workspace_id, project_id=payload.project_id, run_id=payload.run_id
    )


def _made_in_the_sandbox(
    result: SandboxResult,
    spec: PlatformSpec,
    operation: str,
    *,
    code: str,
    requirements: Sequence[str] = (),
) -> _Image:
    """The image a sandbox run made, or the failure whose short reason the graph reads."""
    image = _image_left_by(result, spec)
    if image is None:
        raise failure(steps.OPERATION_FAILED, _why_no_image(result, operation))
    return _Image(
        data=image.data,
        mime=image.mime,
        width=image.width,
        height=image.height,
        cost_usd=Decimal(0),
        provider=SANDBOX_PROVIDER,
        model=None,
        params={
            "operation": operation,
            "duration_s": round(result.duration_s, 3),
            "requirements": list(requirements),
        },
        code=code,
    )


def _image_left_by(result: SandboxResult, spec: PlatformSpec) -> SandboxFile | None:
    """The run's image: the first at the platform's size, else the first it left at all."""
    if not result.succeeded or not result.files:
        return None
    at_size = [f for f in result.files if (f.width, f.height) == (spec.width, spec.height)]
    return (at_size or list(result.files))[0]


def _why_no_image(result: SandboxResult, operation: str) -> str:
    """Why a sandbox run made no image, for the studio's model to fix: the program's own error
    stream, cut short (it holds no secret: the sandbox is given none), or what was refused."""
    what = "the page" if operation == "render_html" else "the program"
    if result.timed_out:
        return f"{what} did not finish within its time limit"
    if result.exit_code != 0:
        printed = result.logs.stderr.strip()[-MAX_PRINTED_CHARS:]
        reason = f"{what} exited with status {result.exit_code}"
        return _cut(f"{reason}: {printed}" if printed else reason)
    refused = "; ".join(result.rejected_outputs)
    reason = f"{what} left no accepted image in /out"
    return _cut(f"{reason}: {refused}" if refused else reason)


def _cut(text: str) -> str:
    return text[:MAX_ERROR_CHARACTERS]


def _answer(version: MediaAssetVersion) -> Mapping[str, Any]:
    """What the graph is told of a stored version: its ids, its size and what it cost."""
    return {
        "version_id": str(version.id),
        "asset_id": str(version.asset_id),
        "width": version.width,
        "height": version.height,
        "mime": version.mime,
        "size_bytes": version.size_bytes,
        "cost_usd": float(version.cost_usd),
    }


def _last_review_of_each_version(
    reviews: Sequence[rules.VersionReview],
) -> list[rules.VersionReview]:
    last = {review.version_id: review for review in reviews}
    return list(last.values())
