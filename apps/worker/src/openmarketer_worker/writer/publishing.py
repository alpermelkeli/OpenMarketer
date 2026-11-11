"""Publishing an approved post from the project's connected account, once its send is begun.

``publish_post``'s handler (``writing.py``) checks the post against what the person approved,
has ``PostPublishing.prepare`` resolve the installation's app on the platform and open the
account's token for the run's workspace, both at this attempt and before anything is begun,
and records, in a transaction it commits, that the send starts (core's ``begin_publishing``).
``PostPublishing.complete`` does the rest, outside that transaction: it reads the post's image
from object storage when it has one and a send needs it, and follows
core's rule of a retry (``social_accounts/publishing.next_publishing_step``) so that a post is
published once whatever earlier attempts did: an attempt that finds the send begun looks the
post up before sending, and a send refused as a duplicate is looked up too. The post the
platform has is recorded ``published`` with what it was reckoned to cost, in a short
transaction of its own.

How it fails, each as a failure with a type and a message that may be shown:

- The platform refused the send (the text, the image, the app's permissions, the developer
  account's credit) or no longer accepts the account's token: nothing was published, the item
  is settled ``failed`` with why, and the action is not tried again (``WRITING_FAILED``).
- The platform refused or revoked a lookup: whether an earlier send published the post is not
  known, so the item stays ``publishing`` and the action is not tried again.
- The platform could not be reached, failed or limited the rate, or the object storage could
  not be read (``PUBLISHING_UNAVAILABLE``): the item stays ``publishing`` and the next attempt
  resumes with a lookup, after the wait the platform asked for, up to
  ``policy.LONGEST_PLATFORM_WAIT_SECONDS``.
- The installation's app has no key or secret, or the account's token does not open
  (``prepare``, before the send is begun): the action is not tried again, and the message
  names what to set.

No credential is kept: the app and the token are resolved for one attempt and handed to the
platform's adapter, never logged, put in an error, returned or stored. What a platform wrote is
the adapter's to log; the errors it raises are worded without it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError

from openmarketer_core.credentials.resolution import CredentialUnreadable, EncryptionKeyMissing
from openmarketer_core.db.content_items import (
    ContentItem,
    ContentItemNotFound,
    ContentItemSettled,
    PublishedAsAnotherPost,
    PublishingBegun,
    PublishingNotBegun,
    record_published,
    record_publishing_failed,
)
from openmarketer_core.db.media_assets import MediaAssetVersionNotFound, asset_version
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.session import transaction
from openmarketer_core.db.writing_runs import WritingRunFinished, WritingRunNotFound
from openmarketer_core.social_accounts.catalog import (
    ConnectablePlatform,
    SocialPlatform,
    connectable_platform,
)
from openmarketer_core.social_accounts.connection import (
    AccountTokens,
    ConnectionSecretUnreadable,
    PlatformApp,
)
from openmarketer_core.social_accounts.publishing import (
    PostImageFile,
    PostPublisher,
    PublishedPost,
    PublishingAccessRevoked,
    PublishingAccount,
    PublishingFailure,
    PublishingRefused,
    PublishingSituation,
    PublishingStage,
    PublishingStep,
    PublishingUnavailable,
    RefusalReason,
    lookup_since,
    next_publishing_step,
)
from openmarketer_core.studio.asset_store import AssetStore, AssetStoreUnavailable
from openmarketer_core.writing.writer_agent.definitions import PublishPost
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.writer import steps
from openmarketer_worker.writer.activities import failure
from openmarketer_worker.writer.policy import LONGEST_PLATFORM_WAIT_SECONDS

logger = logging.getLogger(__name__)

IMAGE_UNREADABLE = "the post's image could not be read from object storage"


def app_not_set(connectable: ConnectablePlatform, missing: str, variable: str) -> str:
    return (
        f"publishing on {connectable.name} needs the installation's app: set {missing} in the "
        f"dashboard's Settings, or with the environment variable {variable}"
    )


def account_gone(connectable: ConnectablePlatform) -> str:
    return (
        f"the project has no {connectable.name} account connected any more: connect it in the "
        "project's Accounts and request the post again"
    )


def account_replaced(connectable: ConnectablePlatform) -> str:
    return (
        f"the project's {connectable.name} account is not the one the post was approved for: "
        "request the post again"
    )


def access_revoked(connectable: ConnectablePlatform) -> str:
    return (
        f"{connectable.name} no longer accepts the account's access: reconnect the "
        f"{connectable.name} account in the project's Accounts and request the post again"
    )


def outcome_unknown(error: Exception, connectable: ConnectablePlatform) -> str:
    return (
        f"{error}; {connectable.name} may have published the post: check the account before "
        "requesting it again"
    )


@dataclass(frozen=True)
class PreparedSend:
    """What one attempt publishes with: the post, the account, the app and the token.

    Held by the attempt only; neither the app nor the token is in a ``repr``.
    """

    post: PublishPost
    connectable: ConnectablePlatform
    account: PublishingAccount
    app: PlatformApp
    tokens: AccountTokens
    publisher: PostPublisher


class PostPublishing:
    """What ``publish_post``'s handler publishes a post with: ``prepare`` before the send is
    begun, ``complete`` once it is."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        credentials: WorkspaceCredentials,
        assets: AssetStore,
        publishers: Mapping[SocialPlatform, PostPublisher],
    ) -> None:
        """``publishers`` is each platform's adapter."""
        self._sessions = sessions
        self._credentials = credentials
        self._assets = assets
        self._publishers = publishers

    def prepare(self, post: PublishPost, platform: SocialPlatform) -> PreparedSend:
        """The app and the token ``post`` is published on ``platform`` with, resolved now.

        Before the send is begun, so that an app without its credentials or a token that does
        not open leaves the post as it was. Raises an ``ApplicationError`` (``WRITING_FAILED``)
        naming what to set, or saying the account the post names is not the project's any more.
        """
        connectable = connectable_platform(platform.value)
        if post.account_id is None or post.username is None:
            raise failure(steps.WRITING_FAILED, account_gone(connectable))
        return PreparedSend(
            post=post,
            connectable=connectable,
            account=PublishingAccount(account_id=post.account_id, username=post.username),
            app=self._app(post.workspace_id, connectable),
            tokens=self._tokens(post, connectable),
            publisher=self._publishers[platform],
        )

    def complete(self, send: PreparedSend, begun: PublishingBegun) -> ContentItem:
        """Publish the post once and record it ``published``; the item as recorded.

        ``begun`` is what ``begin_publishing`` answered, committed. Raises an
        ``ApplicationError`` as the module says.
        """
        post = send.post
        if begun.stage is PublishingStage.DONE:
            return begun.item
        started_at = begun.item.publishing_started_at
        if started_at is None:
            raise failure(steps.WRITING_FAILED, str(PublishingNotBegun(post.content_item_id)))
        since = lookup_since(started_at)
        image: list[PostImageFile | None] = []

        def image_once() -> PostImageFile | None:
            if not image:
                image.append(self._image(post))
            return image[0]

        situation = PublishingSituation(stage=begun.stage)
        while True:
            step = next_publishing_step(situation)
            if step is PublishingStep.NOTHING:
                return begun.item
            if step is PublishingStep.RECORD_FOUND and situation.found is not None:
                return self._record_published(post, situation.found)
            if step is PublishingStep.FAIL_DUPLICATE:
                duplicate = PublishingRefused(send.connectable.name, RefusalReason.DUPLICATE)
                raise self._refused(send, duplicate)
            if step is PublishingStep.LOOK_UP:
                found = self._look_up(send, since)
                situation = replace(situation, looked_up=True, found=found)
                continue
            published = self._send(send, image_once)
            if published is not None:
                return self._record_published(post, published)
            # A lookup made before the send says nothing of what the send did.
            situation = PublishingSituation(stage=begun.stage, refused_as_duplicate=True)

    def _send(
        self, send: PreparedSend, image: Callable[[], PostImageFile | None]
    ) -> PublishedPost | None:
        """The post the send published, or ``None`` when it was refused as a duplicate."""
        try:
            return send.publisher.publish(
                send.app, send.tokens, send.account, text=send.post.text, image=image()
            )
        except PublishingRefused as e:
            if e.duplicate:
                logger.info("a post of content item %s was refused as a duplicate", _item(send))
                return None
            raise self._refused(send, e) from None
        except PublishingAccessRevoked:
            self._record_failed(send.post, PublishingFailure.ACCESS_REVOKED)
            raise failure(steps.WRITING_FAILED, access_revoked(send.connectable)) from None
        except PublishingUnavailable as e:
            raise _unavailable(e, send) from None

    def _look_up(self, send: PreparedSend, since: datetime) -> PublishedPost | None:
        """The account's post with the text since the send began, or ``None``.

        A refused or revoked lookup leaves the item ``publishing``: an earlier send may have
        published the post, which only a lookup can tell.
        """
        try:
            return send.publisher.find_published(
                send.app, send.tokens, send.account, text=send.post.text, since=since
            )
        except (PublishingRefused, PublishingAccessRevoked) as e:
            logger.info("the lookup of content item %s was refused: %s", _item(send), e)
            raise failure(steps.WRITING_FAILED, outcome_unknown(e, send.connectable)) from None
        except PublishingUnavailable as e:
            raise _unavailable(e, send) from None

    def _refused(self, send: PreparedSend, refusal: PublishingRefused) -> ApplicationError:
        """Settle the item ``failed`` for the platform's refusal; the failure to raise."""
        self._record_failed(send.post, PublishingFailure.of_refusal(refusal.reason))
        logger.info("content item %s was refused: %s", _item(send), refusal)
        return failure(steps.WRITING_FAILED, str(refusal))

    def _app(self, workspace_id: uuid.UUID, connectable: ConnectablePlatform) -> PlatformApp:
        """The installation's app on the platform, resolved for the workspace now."""
        wanted = (connectable.app_key, connectable.app_secret)
        try:
            values = self._credentials.values(workspace_id, wanted)
        except CredentialUnreadable as e:
            raise failure(steps.WRITING_FAILED, str(e)) from None
        resolved: list[str] = []
        for credential in wanted:
            value = values[credential]
            if value is None:
                missing = app_not_set(connectable, credential.name, credential.environment_variable)
                raise failure(steps.WRITING_FAILED, missing)
            resolved.append(value)
        key, secret = resolved
        return PlatformApp(key=key, secret=secret)

    def _tokens(self, post: PublishPost, connectable: ConnectablePlatform) -> AccountTokens:
        """The token of the account the post was approved for, opened now."""
        try:
            found = self._credentials.account_tokens(
                post.workspace_id, post.project_id, connectable.platform
            )
        except (EncryptionKeyMissing, ConnectionSecretUnreadable) as e:
            raise failure(steps.WRITING_FAILED, str(e)) from None
        if found is None:
            raise failure(steps.WRITING_FAILED, account_gone(connectable))
        if found.account_id != post.account_id:
            raise failure(steps.WRITING_FAILED, account_replaced(connectable))
        return found.tokens

    def _image(self, post: PublishPost) -> PostImageFile | None:
        """The image of the version the person approved, read now; ``None`` without one."""
        if post.asset_version_id is None:
            return None
        try:
            with transaction(self._sessions) as session:
                version = asset_version(
                    session,
                    workspace_id=post.workspace_id,
                    project_id=post.project_id,
                    version_id=post.asset_version_id,
                )
        except MediaAssetVersionNotFound as e:
            raise failure(steps.WRITING_FAILED, str(e)) from None
        try:
            data = self._assets.get(version.storage_key)
        except AssetStoreUnavailable:
            raise failure(steps.PUBLISHING_UNAVAILABLE, IMAGE_UNREADABLE) from None
        return PostImageFile(data=data, media_type=version.mime)

    def _record_published(self, post: PublishPost, published: PublishedPost) -> ContentItem:
        cost = post.cost_estimate_usd if post.cost_estimate_usd is not None else Decimal(0)
        try:
            with transaction(self._sessions) as session:
                return record_published(
                    session,
                    workspace_id=post.workspace_id,
                    project_id=post.project_id,
                    item_id=post.content_item_id,
                    post=published,
                    cost_usd=cost,
                )
        except _GONE as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except (PublishedAsAnotherPost, PublishingNotBegun) as e:
            raise failure(steps.WRITING_FAILED, str(e)) from None

    def _record_failed(self, post: PublishPost, why: PublishingFailure) -> None:
        try:
            with transaction(self._sessions) as session:
                record_publishing_failed(
                    session,
                    workspace_id=post.workspace_id,
                    project_id=post.project_id,
                    item_id=post.content_item_id,
                    failure=why,
                )
        except _GONE as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except PublishingNotBegun as e:
            raise failure(steps.WRITING_FAILED, str(e)) from None


# The item or its run is not there any more, or was settled another way.
_GONE = (
    ContentItemNotFound,
    ContentItemSettled,
    WritingRunNotFound,
    WritingRunFinished,
    ProjectNotFound,
)


def _unavailable(error: PublishingUnavailable, send: PreparedSend) -> ApplicationError:
    """Another attempt, after the wait the platform asked for when it said one."""
    logger.info("content item %s was not published yet: %s", _item(send), error)
    delay = None
    if error.retry_after_seconds is not None and error.retry_after_seconds > 0:
        delay = timedelta(seconds=min(error.retry_after_seconds, LONGEST_PLATFORM_WAIT_SECONDS))
    return ApplicationError(
        str(error), type=steps.PUBLISHING_UNAVAILABLE, non_retryable=False, next_retry_delay=delay
    )


def _item(send: PreparedSend) -> uuid.UUID:
    return send.post.content_item_id
