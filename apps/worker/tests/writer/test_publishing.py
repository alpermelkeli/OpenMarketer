"""Tests for publishing an approved post from the project's connected X account.

``publish_post``'s handler as a plain function against PostgreSQL, with a fake X that keeps the
account's posts and refuses the same text twice (``writer_fakes.FakeX``), and the graph's steps
run through the activities the worker registers. A post is published once whatever earlier
attempts did, its item records how it ended, and no app key or token leaves the handler but to
the platform's adapter.
"""

import json
import logging
from datetime import timedelta
from decimal import Decimal

import pytest
from temporalio.exceptions import ApplicationError
from writer_fakes import (
    ACCESS_SECRET,
    ACCESS_TOKEN,
    ACME,
    ANSWER_LOST,
    APP_KEY,
    APP_SECRET,
    IMAGE_BYTES,
    POST,
    SECRETS,
    X_APP,
    FakeAssets,
)

from openmarketer_core.agent_runtime.actions import ActionRequest
from openmarketer_core.db.models import ContentItemStatus
from openmarketer_core.social_accounts.catalog import AccountDescription, SocialPlatform
from openmarketer_core.social_accounts.publishing import (
    LOOKUP_MARGIN,
    PostImageFile,
    PublishingAccessRevoked,
    PublishingFailure,
    PublishingRefused,
    PublishingUnavailable,
    RefusalReason,
)
from openmarketer_core.writing.writer_agent.definitions import (
    PUBLISH_POST,
    STORE_DRAFT,
    PublishPost,
)
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.writer import steps
from openmarketer_worker.writer.policy import LONGEST_PLATFORM_WAIT_SECONDS
from openmarketer_worker.writer.publishing import PostPublishing
from openmarketer_worker.writer.writing import WritingWork

OTHER = AccountDescription(
    account_id="77", username="other", display_name="Other", picture_url=None
)


@pytest.fixture
def activities(writer_registration, activities_of):
    return activities_of(writer_registration())


async def approved(activities, writer_database, run) -> tuple[PublishPost, str]:
    """The run's ``publish_post``, held and approved by a person: its payload and action id."""
    asked = (await activities.step(run)).action
    assert asked is not None and asked.name == STORE_DRAFT
    stored = await activities.act(asked)
    request = (await activities.step(run, stored)).action
    assert request is not None and request.name == PUBLISH_POST
    writer_database.held(run, request)
    return PublishPost.model_validate(request.payload), request.action_id


async def approved_from_the_account(
    activities, writer_database, cipher, *, with_image: bool = False
):
    """A started run of a project whose X account is connected, its post approved."""
    asked = writer_database.requested(started=True, with_image=with_image)
    writer_database.connect(asked.run, cipher)
    payload, action_id = await approved(activities, writer_database, asked.run)
    return asked, payload, action_id


def refused(work, payload, action_id) -> ApplicationError:
    with pytest.raises(ApplicationError) as failure:
        work.publish_post(payload, action_id)
    return failure.value


def work_with(writer_parts, **changes) -> WritingWork:
    parts = writer_parts(**changes)
    return WritingWork(
        parts.sessions,
        parts.checkpoints,
        new_model=parts.new_model,
        limits=parts.limits,
        publishing=parts.publishing,
        publishing_costs=parts.publishing_costs,
    )


# --------------------------------------------------------------------------- the proposal
async def test_post_of_a_project_with_a_connected_account_names_it_and_the_estimate(
    activities, writer_database, cipher
):
    _, payload, _ = await approved_from_the_account(activities, writer_database, cipher)

    assert (payload.account_id, payload.username) == (ACME.account_id, ACME.username)
    assert payload.cost_estimate_usd == Decimal("0.015")
    assert not payload.dry_run


async def test_post_of_a_project_without_an_account_is_still_a_dry_run_and_sends_nothing(
    work, activities, writer_database, x
):
    asked = writer_database.requested(started=True)
    payload, action_id = await approved(activities, writer_database, asked.run)

    answer = work.publish_post(payload, action_id)

    assert payload.dry_run
    assert answer["status"] == "dry_run"
    assert writer_database.item(asked.run).status is ContentItemStatus.DRY_RUN
    assert x.publish_calls == [] and x.lookup_calls == []


# ------------------------------------------------------------------------------ published
async def test_approved_post_is_published_from_the_account_and_recorded_with_its_cost(
    work, activities, writer_database, cipher, x
):
    asked, payload, action_id = await approved_from_the_account(activities, writer_database, cipher)

    answer = work.publish_post(payload, action_id)

    item = writer_database.item(asked.run)
    assert answer == {"content_item_id": str(item.id), "status": "published"}
    ((text, post),) = x.posts
    assert text == POST
    assert (item.status, item.external_id, item.external_url) == (
        ContentItemStatus.PUBLISHED,
        post.external_id,
        post.url,
    )
    assert (item.published_account_id, item.published_username) == ("42", "acme")
    assert item.publishing_cost_usd == Decimal("0.015")
    assert item.proposal_id is not None and item.published_at is not None
    (sent,) = x.publish_calls
    assert (sent.app.key, sent.app.secret) == (APP_KEY, APP_SECRET)
    assert (sent.tokens.access_token, sent.tokens.access_secret) == (ACCESS_TOKEN, ACCESS_SECRET)
    assert sent.image is None
    assert x.lookup_calls == []  # a send nobody began before needs no lookup


async def test_approved_post_with_an_image_is_published_with_the_approved_versions_bytes(
    work, activities, writer_database, cipher, x, assets
):
    asked, payload, action_id = await approved_from_the_account(
        activities, writer_database, cipher, with_image=True
    )
    assert asked.image is not None and payload.asset_version_id == asked.image.version_id
    writer_database.store_image(asked.run, asked.image, assets)

    work.publish_post(payload, action_id)

    (sent,) = x.publish_calls
    assert sent.image == PostImageFile(data=IMAGE_BYTES, media_type="image/png")
    assert writer_database.item(asked.run).status is ContentItemStatus.PUBLISHED


async def test_post_published_again_sends_nothing_and_answers_the_same(
    work, activities, writer_database, cipher, x
):
    asked, payload, action_id = await approved_from_the_account(activities, writer_database, cipher)
    first = work.publish_post(payload, action_id)

    again = work.publish_post(payload, action_id)

    assert again == first
    assert len(x.publish_calls) == 1 and x.lookup_calls == []


# ------------------------------------------------------------------- the account changed
async def test_post_whose_account_was_disconnected_is_refused_for_good_and_never_a_dry_run(
    work, activities, writer_database, cipher, x
):
    asked, payload, action_id = await approved_from_the_account(activities, writer_database, cipher)
    writer_database.disconnect(asked.run)

    failure = refused(work, payload, action_id)

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)
    assert "no X account connected" in failure.message
    assert writer_database.item(asked.run).status is ContentItemStatus.PROPOSED
    assert x.publish_calls == []


async def test_post_whose_account_was_replaced_is_refused_for_good_and_sent_nowhere(
    work, activities, writer_database, cipher, x
):
    asked, payload, action_id = await approved_from_the_account(activities, writer_database, cipher)
    writer_database.connect(asked.run, cipher, OTHER)

    failure = refused(work, payload, action_id)

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)
    assert "not the one the post was approved for" in failure.message
    assert writer_database.item(asked.run).status is ContentItemStatus.PROPOSED
    assert x.publish_calls == []


# ------------------------------------------------------------------ never twice
async def test_attempt_that_finds_the_send_begun_and_the_post_published_records_it_unsent(
    work, activities, writer_database, cipher, x
):
    asked, payload, action_id = await approved_from_the_account(activities, writer_database, cipher)
    x.sends = [ANSWER_LOST]  # X published the post, and its answer never came
    lost = refused(work, payload, action_id)
    begun_at = writer_database.item(asked.run).publishing_started_at

    answer = work.publish_post(payload, action_id)

    assert (lost.type, lost.non_retryable) == (steps.PUBLISHING_UNAVAILABLE, False)
    assert answer["status"] == "published"
    ((_, post),) = x.posts
    assert writer_database.item(asked.run).external_id == post.external_id
    assert len(x.publish_calls) == 1  # the lost send only
    (lookup,) = x.lookup_calls
    assert begun_at is not None and lookup.since == begun_at - LOOKUP_MARGIN


async def test_attempt_that_finds_the_send_begun_and_no_post_sends_it_once(
    work, activities, writer_database, cipher, x
):
    asked, payload, action_id = await approved_from_the_account(activities, writer_database, cipher)
    x.sends = [PublishingUnavailable("X could not be reached")]  # nothing was published
    first = refused(work, payload, action_id)

    work.publish_post(payload, action_id)

    assert first.type == steps.PUBLISHING_UNAVAILABLE
    assert writer_database.item(asked.run).status is ContentItemStatus.PUBLISHED
    assert len(x.posts) == 1
    assert len(x.publish_calls) == 2 and len(x.lookup_calls) == 1


async def test_platform_unavailable_leaves_the_post_publishing_and_another_attempt_publishes_once(
    work, activities, writer_database, cipher, x
):
    asked, payload, action_id = await approved_from_the_account(activities, writer_database, cipher)
    x.sends = [ANSWER_LOST]
    x.lookups = [PublishingUnavailable("X answered HTTP 503")]

    attempts = []
    for _ in range(2):
        attempts.append(refused(work, payload, action_id))
        assert writer_database.item(asked.run).status is ContentItemStatus.PUBLISHING
    answer = work.publish_post(payload, action_id)

    assert [a.type for a in attempts] == [steps.PUBLISHING_UNAVAILABLE] * 2
    assert not any(a.non_retryable for a in attempts)
    assert answer["status"] == "published"
    assert len(x.posts) == 1  # published once, by the send whose answer was lost
    assert len(x.publish_calls) == 1 and len(x.lookup_calls) == 2


async def test_wait_the_platform_asked_for_is_the_next_attempts_up_to_the_longest(
    work, activities, writer_database, cipher, x
):
    asked, payload, action_id = await approved_from_the_account(activities, writer_database, cipher)
    x.sends = [
        PublishingUnavailable("X limits the rate", retry_after_seconds=60),
        PublishingUnavailable("X limits the rate", retry_after_seconds=24 * 60 * 60),
        PublishingUnavailable("X answered HTTP 503"),
    ]

    asked_for, too_long, unsaid = (refused(work, payload, action_id) for _ in range(3))

    assert asked_for.next_retry_delay == timedelta(seconds=60)
    assert too_long.next_retry_delay == timedelta(seconds=LONGEST_PLATFORM_WAIT_SECONDS)
    assert unsaid.next_retry_delay is None


async def test_send_refused_as_a_duplicate_is_looked_up_and_the_post_found_recorded(
    work, activities, writer_database, cipher, x
):
    asked, payload, action_id = await approved_from_the_account(activities, writer_database, cipher)
    earlier = x.has_published(POST)  # an earlier send published it, unanswered

    answer = work.publish_post(payload, action_id)

    assert answer["status"] == "published"
    assert writer_database.item(asked.run).external_id == earlier.external_id
    assert len(x.posts) == 1 and len(x.publish_calls) == 1 and len(x.lookup_calls) == 1


async def test_send_refused_as_a_duplicate_of_a_post_not_found_is_settled_failed(
    work, activities, writer_database, cipher, x
):
    asked, payload, action_id = await approved_from_the_account(activities, writer_database, cipher)
    x.has_published(POST)
    x.hidden = {POST}  # a person published the text before this send began

    failure = refused(work, payload, action_id)

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)
    assert "already published this text" in failure.message
    item = writer_database.item(asked.run)
    assert (item.status, item.publishing_failure) == (
        ContentItemStatus.FAILED,
        PublishingFailure.DUPLICATE,
    )


# ---------------------------------------------------------------------- refused, revoked
@pytest.mark.parametrize(
    ("reason", "said"),
    [
        (RefusalReason.TOO_LONG, "too long"),
        (RefusalReason.MEDIA_REFUSED, "refused the image"),
        (RefusalReason.NOT_PERMITTED, "check the app's permissions"),
        (RefusalReason.OUT_OF_CREDITS, "no credit left"),
    ],
)
async def test_send_the_platform_refused_settles_the_post_failed_with_why(
    work, activities, writer_database, cipher, x, reason, said
):
    asked, payload, action_id = await approved_from_the_account(activities, writer_database, cipher)
    x.sends = [PublishingRefused("X", reason)]

    failure = refused(work, payload, action_id)

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)
    assert said in failure.message
    item = writer_database.item(asked.run)
    assert (item.status, item.publishing_failure) == (
        ContentItemStatus.FAILED,
        PublishingFailure.of_refusal(reason),
    )
    assert x.posts == []


async def test_account_whose_access_was_revoked_settles_the_post_failed_saying_reconnect(
    work, activities, writer_database, cipher, x
):
    asked, payload, action_id = await approved_from_the_account(activities, writer_database, cipher)
    x.sends = [PublishingAccessRevoked("X")]

    failure = refused(work, payload, action_id)

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)
    assert "reconnect the X account in the project's Accounts" in failure.message
    item = writer_database.item(asked.run)
    assert (item.status, item.publishing_failure) == (
        ContentItemStatus.FAILED,
        PublishingFailure.ACCESS_REVOKED,
    )


async def test_lookup_the_platform_refused_leaves_the_post_publishing_saying_it_may_be_there(
    work, activities, writer_database, cipher, x
):
    asked, payload, action_id = await approved_from_the_account(activities, writer_database, cipher)
    x.sends = [ANSWER_LOST]
    refused(work, payload, action_id)
    x.lookups = [PublishingRefused("X", RefusalReason.OUT_OF_CREDITS)]

    failure = refused(work, payload, action_id)

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)
    assert "may have published the post" in failure.message
    assert writer_database.item(asked.run).status is ContentItemStatus.PUBLISHING
    assert len(x.publish_calls) == 1


async def test_image_object_storage_cannot_read_is_worth_another_attempt(
    work, activities, writer_database, cipher, x, assets
):
    asked, payload, action_id = await approved_from_the_account(
        activities, writer_database, cipher, with_image=True
    )
    assert asked.image is not None
    writer_database.store_image(asked.run, asked.image, assets)
    assets.down = True

    failure = refused(work, payload, action_id)
    assets.down = False
    work.publish_post(payload, action_id)

    assert (failure.type, failure.non_retryable) == (steps.PUBLISHING_UNAVAILABLE, False)
    assert len(x.posts) == 1


# ----------------------------------------------------------------------------- credentials
@pytest.mark.parametrize(
    ("environ", "variable"),
    [({}, "X_API_KEY"), ({"X_API_KEY": APP_KEY}, "X_API_SECRET")],
)
async def test_app_without_its_credentials_refuses_the_post_for_good_naming_what_to_set(
    writer_parts, activities, writer_database, cipher, x, environ, variable
):
    asked, payload, action_id = await approved_from_the_account(activities, writer_database, cipher)
    work = work_with(writer_parts, environ=environ)

    failure = refused(work, payload, action_id)

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)
    assert variable in failure.message
    # Nothing was begun: the post stays proposed, and the run's failure settles it.
    assert writer_database.item(asked.run).status is ContentItemStatus.PROPOSED
    assert x.publish_calls == []


async def test_token_that_does_not_open_refuses_the_post_for_good(
    writer_parts, activities, writer_database, cipher, another_cipher, x
):
    asked, payload, action_id = await approved_from_the_account(activities, writer_database, cipher)
    # The installation's ENCRYPTION_KEY is not the one the token was sealed with.
    parts = writer_parts()
    publishing = PostPublishing(
        parts.sessions,
        WorkspaceCredentials(parts.sessions, X_APP, another_cipher),
        FakeAssets(),
        {SocialPlatform.X: x},
    )
    work = WritingWork(
        parts.sessions,
        parts.checkpoints,
        new_model=parts.new_model,
        limits=parts.limits,
        publishing=publishing,
        publishing_costs=parts.publishing_costs,
    )

    failure = refused(work, payload, action_id)

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)
    assert "ENCRYPTION_KEY" in failure.message
    assert writer_database.item(asked.run).status is ContentItemStatus.PROPOSED


async def test_no_app_key_or_token_is_in_an_answer_a_failure_or_the_log(
    work, activities, writer_database, cipher, x, caplog
):
    caplog.set_level(logging.DEBUG)
    shown: list[str] = []
    for sends in (
        [PublishingUnavailable("X limits the rate", retry_after_seconds=5)],
        [PublishingAccessRevoked("X")],
    ):
        asked, payload, action_id = await approved_from_the_account(
            activities, writer_database, cipher
        )
        x.posts.clear()
        x.sends = list(sends)
        for _ in sends:
            failure = refused(work, payload, action_id)
            shown += [failure.message, repr(failure), str(failure.details)]
        if writer_database.item(asked.run).status is ContentItemStatus.PUBLISHING:
            shown.append(json.dumps(work.publish_post(payload, action_id)))
        shown.append(repr(writer_database.item(asked.run)))

    assert x.publish_calls  # the secrets did reach the platform's adapter
    everything = "\n".join([*shown, caplog.text])
    for secret in SECRETS:
        assert secret not in everything


async def test_request_the_gate_held_carries_no_app_key_or_token(
    activities, writer_database, cipher
):
    asked = writer_database.requested(started=True)
    writer_database.connect(asked.run, cipher)
    stored = await activities.act((await activities.step(asked.run)).action)
    request: ActionRequest | None = (await activities.step(asked.run, stored)).action

    assert request is not None
    held = json.dumps(request.payload)
    for secret in SECRETS:
        assert secret not in held
