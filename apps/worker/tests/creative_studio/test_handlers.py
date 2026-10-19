"""Tests for the handlers of the studio's actions, run as plain functions against PostgreSQL.

Each makes an image with a fake provider or a fake sandbox, stores it in a dictionary as
object storage, and its row in the test's database; skipped when PostgreSQL is not reachable.
"""

import uuid

import pytest
from studio_fakes import ESTIMATE, IMAGE_COST, a_failed_program, a_png, size_of
from temporalio.exceptions import ApplicationError

from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.media_assets import asset_version
from openmarketer_core.db.repository_images import remove_repository_image
from openmarketer_core.db.session import transaction
from openmarketer_core.studio.media_provider import (
    MediaAccountLimited,
    MediaRefused,
    MediaUnavailable,
)
from openmarketer_core.studio.openrouter_images import ACCOUNT_LIMITED
from openmarketer_core.studio.sandbox import SandboxUnavailable
from openmarketer_core.studio.studio_agent.definitions import (
    EditImage,
    GenerateImage,
    ReadText,
    RenderHtml,
    RunPython,
)
from openmarketer_core.studio.studio_agent.rules import BLOCKED, Technique
from openmarketer_core.studio.workflow_contract import STUDIO_RUN_KIND
from openmarketer_worker.agent_runtime.routes import ActionContext
from openmarketer_worker.agent_runtime.vocabulary import DATABASE_UNAVAILABLE, MAX_ERROR_CHARACTERS
from openmarketer_worker.creative_studio import steps


def an_action() -> ActionContext:
    return ActionContext(action_id=f"studio-run:{uuid.uuid4()}:{uuid.uuid4().hex}")


def generation(run, *, asset_id=None, **fields) -> GenerateImage:
    return GenerateImage(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        run_id=run.run_id,
        version_id=uuid.uuid4(),
        asset_id=asset_id,
        round=1,
        technique=Technique.GENERATED,
        prompt="A bright desk at dawn.",
        **fields,
    )


def a_program(run, *, asset_id=None, **fields) -> RunPython:
    return RunPython(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        run_id=run.run_id,
        version_id=uuid.uuid4(),
        asset_id=asset_id,
        round=1,
        technique=Technique.COMPOSED,
        code="print('hello')",
        **fields,
    )


@pytest.fixture
def run(studio_database):
    """A studio run a worker has picked up."""
    return studio_database.requested(started=True).run


def stored_version(sessions, run, version_id):
    with transaction(sessions) as session:
        return asset_version(
            session,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            version_id=version_id,
        )


def refusal(call) -> ApplicationError:
    with pytest.raises(ApplicationError) as refused:
        call()
    return refused.value


# ------------------------------------------------------------------ paid images
def test_generated_image_is_stored_as_the_version_its_payload_names(work, run, assets, sessions):
    payload, action = generation(run), an_action()

    answer = work.generate_image(payload, action)

    stored = stored_version(sessions, run, payload.version_id)
    assert answer == {
        "version_id": str(payload.version_id),
        "asset_id": str(stored.asset_id),
        "width": 1080,
        "height": 1080,
        "mime": "image/png",
        "size_bytes": stored.size_bytes,
        "cost_usd": float(IMAGE_COST),
    }
    assert (stored.action_id, stored.cost_usd, stored.prompt) == (
        action.action_id,
        IMAGE_COST,
        payload.prompt,
    )
    assert list(assets.objects) == [stored.storage_key]


def test_generation_tried_again_stores_one_version_and_pays_once(work, run, media, assets):
    payload, action = generation(run), an_action()

    first = work.generate_image(payload, action)
    again = work.generate_image(payload, action)

    assert again == first
    assert len(media.calls) == 1
    assert len(assets.objects) == 1


def test_first_version_makes_the_runs_asset_and_the_next_one_joins_it(work, run):
    first = work.generate_image(generation(run), an_action())

    second = work.generate_image(
        generation(run, asset_id=uuid.UUID(first["asset_id"])), an_action()
    )

    assert second["asset_id"] == first["asset_id"]
    assert second["version_id"] != first["version_id"]


def test_image_of_another_size_is_fitted_to_the_platforms(work, run, media, assets, sessions):
    media.size = (512, 768)
    payload = generation(run)

    answer = work.generate_image(payload, an_action())

    stored = stored_version(sessions, run, payload.version_id)
    assert (answer["width"], answer["height"]) == (1080, 1080)
    assert size_of(assets.objects[stored.storage_key]) == (1080, 1080)


def test_references_are_the_runs_versions_and_the_projects_assets(
    work, run, media, assets, sessions
):
    first = work.generate_image(generation(run), an_action())
    first_image = assets.objects[
        stored_version(sessions, run, uuid.UUID(first["version_id"])).storage_key
    ]

    work.generate_image(
        generation(
            run,
            asset_id=uuid.UUID(first["asset_id"]),
            reference_version_ids=[uuid.UUID(first["version_id"])],
            reference_asset_ids=[uuid.UUID(first["asset_id"])],
        ),
        an_action(),
    )

    assert media.calls[-1]["references"] == [first_image, first_image]


def test_reference_that_is_not_an_image_of_the_run_is_refused_before_paying(
    work, run, studio_database, media
):
    elsewhere = studio_database.requested(started=True).run
    theirs = work.generate_image(generation(elsewhere), an_action())

    refused = refusal(
        lambda: work.generate_image(
            generation(run, reference_version_ids=[uuid.UUID(theirs["version_id"])]),
            an_action(),
        )
    )

    assert (refused.type, refused.non_retryable) == (steps.OPERATION_FAILED, True)
    assert len(media.calls) == 1


def test_edit_is_given_the_runs_version_it_changes(work, run, media, assets, sessions):
    first = work.generate_image(generation(run), an_action())
    source = uuid.UUID(first["version_id"])
    payload = EditImage(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        run_id=run.run_id,
        version_id=uuid.uuid4(),
        asset_id=uuid.UUID(first["asset_id"]),
        round=2,
        technique=Technique.GENERATED,
        source_version_id=source,
        instruction="Make the light warmer.",
    )

    work.edit_image(payload, an_action())

    assert media.calls[-1]["operation"] == "edit"
    assert (
        media.calls[-1]["source"]
        == assets.objects[stored_version(sessions, run, source).storage_key]
    )


def test_image_the_provider_refused_fails_the_operation_for_good(work, run, media):
    media.failures = [MediaRefused("the image model refused to make this image")]

    refused = refusal(lambda: work.generate_image(generation(run), an_action()))

    assert (refused.type, refused.non_retryable) == (steps.OPERATION_FAILED, True)
    assert refused.message == "the image model refused to make this image"


def test_provider_that_cannot_be_reached_may_pass(work, run, media):
    media.failures = [MediaUnavailable("the image provider could not be reached: ConnectError")]

    refused = refusal(lambda: work.generate_image(generation(run), an_action()))

    assert (refused.type, refused.non_retryable) == (steps.STUDIO_UNAVAILABLE, False)


def test_key_that_cannot_pay_blocks_the_run_for_good_and_releases_its_spend(work, run, media):
    media.failures = [MediaAccountLimited(ACCOUNT_LIMITED)]

    refused = refusal(lambda: work.generate_image(generation(run), an_action()))

    # A type the handler worded and that is not the budget's "may have spent": released.
    assert (refused.type, refused.non_retryable) == (steps.OPERATION_FAILED, True)
    assert refused.message == BLOCKED + ACCOUNT_LIMITED


def test_paid_image_object_storage_lost_counts_against_the_budget(work, run, assets):
    assets.unavailable = True

    refused = refusal(lambda: work.generate_image(generation(run), an_action()))

    # The type the creative budget commits the estimate for.
    assert (refused.type, refused.message) == (DATABASE_UNAVAILABLE, steps.VERSION_NOT_STORED)


def test_version_of_a_run_never_started_is_not_stored(work, studio_database, assets):
    queued = studio_database.requested(started=False).run

    refused = refusal(lambda: work.generate_image(generation(queued), an_action()))

    assert (refused.type, refused.non_retryable) == (steps.RUN_UNAVAILABLE, True)
    assert assets.objects == {}


def test_estimate_of_a_paid_image_is_the_image_roles(work, run):
    assert work.estimate(run.workspace_id) == ESTIMATE


# ------------------------------------------------------------------ the sandbox
def test_program_is_given_the_versions_it_names_as_png_files_beside_it(work, run, sandbox):
    first = work.generate_image(generation(run), an_action())
    named = uuid.UUID(first["version_id"])

    answer = work.run_python(
        a_program(run, asset_id=uuid.UUID(first["asset_id"]), input_version_ids=[named]),
        an_action(),
    )

    assert list(sandbox.runs[0]["inputs"]) == [f"{named}.png"]
    assert (answer["width"], answer["height"], answer["cost_usd"]) == (1080, 1080, 0.0)


def test_program_that_failed_is_told_with_a_short_reason_and_stores_nothing(
    work, run, sandbox, assets
):
    printed = "Traceback (most recent call last):\n" + "x" * 2000 + "\nNameError: name 'y'"
    sandbox.result = a_failed_program(printed)

    refused = refusal(lambda: work.run_python(a_program(run), an_action()))

    assert (refused.type, refused.non_retryable) == (steps.OPERATION_FAILED, True)
    assert refused.message.startswith("the program exited with status 1: ")
    assert refused.message.endswith("NameError: name 'y'")
    assert len(refused.message) <= MAX_ERROR_CHARACTERS
    assert assets.objects == {}


NO_SANDBOX = "the sandbox could not be started: the docker command was not found"


def test_sandbox_that_cannot_start_may_pass_and_blocks_the_run_if_it_does_not(
    work, run, sandbox, assets
):
    sandbox.failures = [SandboxUnavailable(NO_SANDBOX)]

    refused = refusal(lambda: work.run_python(a_program(run), an_action()))

    assert (refused.type, refused.non_retryable) == (steps.STUDIO_UNAVAILABLE, False)
    assert refused.message == BLOCKED + NO_SANDBOX
    assert assets.objects == {}


def test_text_that_cannot_be_read_for_want_of_a_sandbox_is_told_as_blocking(work, run, sandbox):
    first = work.generate_image(generation(run), an_action())
    sandbox.failures = [SandboxUnavailable(NO_SANDBOX)]
    payload = ReadText(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        run_id=run.run_id,
        version_id=uuid.UUID(first["version_id"]),
    )

    refused = refusal(lambda: work.read_text(payload, an_action()))

    assert (refused.type, refused.message) == (steps.STUDIO_UNAVAILABLE, BLOCKED + NO_SANDBOX)


def test_page_is_rendered_at_the_platforms_size_and_stored(work, run, sandbox, sessions):
    payload = RenderHtml(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        run_id=run.run_id,
        version_id=uuid.uuid4(),
        asset_id=None,
        round=1,
        technique=Technique.HTML_REFERENCE,
        html="<h1>Launch day</h1>",
    )

    work.render_html(payload, an_action())

    stored = stored_version(sessions, run, payload.version_id)
    assert (stored.width, stored.height, stored.code) == (1080, 1080, "<h1>Launch day</h1>")
    assert sandbox.runs[0]["operation"] == "render_html"


def test_text_of_a_version_is_what_ocr_read(work, run, sandbox):
    first = work.generate_image(generation(run), an_action())
    payload = ReadText(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        run_id=run.run_id,
        version_id=uuid.UUID(first["version_id"]),
    )

    assert work.read_text(payload, an_action()) == {"text": "Launch day"}


# ------------------------------------------------------------ the product's own images
ICON = a_png(256, 256, colour=(10, 10, 200))
SCREENSHOT = a_png(300, 600, colour=(10, 200, 10))


def test_an_attempts_context_lists_the_projects_repository_images_without_their_bytes(
    work, run, studio_database, assets, checkpoints
):
    icon, shot = studio_database.repository_images(
        run, assets, ("icon", ICON), ("screenshot", SCREENSHOT)
    )

    context = work.context(GraphRunRef(STUDIO_RUN_KIND, run.run_id), checkpoints)

    shown = {
        (image.id, image.kind, image.width, image.height) for image in context.repository_images
    }
    assert shown == {(str(icon), "icon", 256, 256), (str(shot), "screenshot", 300, 600)}
    found = context.repository_image_of(str(icon))
    assert found is not None and found.data == ICON
    assert context.repository_image_of(str(uuid.uuid4())) is None


def test_an_attempts_context_leaves_out_the_repository_images_a_person_removed(
    work, run, studio_database, assets, checkpoints
):
    icon, shot = studio_database.repository_images(
        run, assets, ("icon", ICON), ("screenshot", SCREENSHOT)
    )
    with transaction(studio_database.sessions) as session:
        remove_repository_image(
            session,
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            image_id=shot,
            removed_by=uuid.uuid4(),
        )

    context = work.context(GraphRunRef(STUDIO_RUN_KIND, run.run_id), checkpoints)

    assert [image.id for image in context.repository_images] == [str(icon)]
    assert context.repository_image_of(str(shot)) is None


def test_a_run_whose_project_has_no_repository_images_has_an_empty_list(work, run, checkpoints):
    context = work.context(GraphRunRef(STUDIO_RUN_KIND, run.run_id), checkpoints)

    assert list(context.repository_images) == []


def test_repository_images_are_sent_to_the_image_model_after_the_other_references(
    work, run, studio_database, media, assets, sessions
):
    [icon] = studio_database.repository_images(run, assets, ("icon", ICON))
    first = work.generate_image(generation(run), an_action())
    first_image = assets.objects[
        stored_version(sessions, run, uuid.UUID(first["version_id"])).storage_key
    ]

    work.generate_image(
        generation(
            run,
            asset_id=uuid.UUID(first["asset_id"]),
            reference_version_ids=[uuid.UUID(first["version_id"])],
            reference_repository_image_ids=[icon],
        ),
        an_action(),
    )

    assert media.calls[-1]["references"] == [first_image, ICON]


def test_repository_image_of_another_project_is_refused_before_paying(
    work, run, studio_database, media, assets
):
    elsewhere = studio_database.requested(started=True).run
    [theirs] = studio_database.repository_images(elsewhere, assets, ("icon", ICON))

    refused = refusal(
        lambda: work.generate_image(
            generation(run, reference_repository_image_ids=[theirs]), an_action()
        )
    )

    assert (refused.type, refused.non_retryable) == (steps.OPERATION_FAILED, True)
    assert refused.message == f"{theirs} is not a repository image of this project"
    assert media.calls == []


def test_program_is_given_the_repository_images_it_names_beside_it(
    work, run, studio_database, sandbox, assets
):
    [shot] = studio_database.repository_images(run, assets, ("screenshot", SCREENSHOT))

    work.run_python(a_program(run, input_repository_image_ids=[shot]), an_action())

    assert sandbox.runs[0]["inputs"] == {f"{shot}.png": SCREENSHOT}
