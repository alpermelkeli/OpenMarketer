"""Publishing a post from a project's connected account: the port and the rule of a retry.

``PostPublisher`` is what a platform's adapter does to publish: send a post's text, with an
image when it has one, as an account, and find a post the account already published with a
given text, for when the outcome of an earlier send is not known. A new platform brings an
adapter in its own folder; nothing here names one.

An adapter raises one of three errors, whose messages say what failed and never repeat what
the platform answered:

- ``PublishingAccessRevoked``: the platform no longer accepts the account's token (the person
  removed the app's access, or the token was revoked); the account must be connected again.
- ``PublishingRefused``: the platform refused the post, for a ``RefusalReason`` the adapter
  read from the answer; nothing was published. ``RefusalReason.DUPLICATE`` is a refusal of
  its own kind: the account published this text already, which may be the post an earlier,
  unanswered send published.
- ``PublishingUnavailable``: the platform could not be reached, failed, or limits the rate;
  the post may or may not have been published, and the send may be tried again later.

**Never twice.** A send whose answer is lost (a timeout, a worker that stops) may have
published the post. The code that publishes therefore records that a send started before it
sends, and ``next_publishing_step`` decides what a later attempt does from what it finds:

    stage STARTED (nothing was sent before)            -> SEND
    stage RESUMED (a send started and its outcome is unknown)
        not looked up yet                              -> LOOK_UP
        the account's post with this text was found    -> RECORD_FOUND
        none was found                                 -> SEND
    the platform refused the send as a duplicate
        not looked up yet                              -> LOOK_UP
        found                                          -> RECORD_FOUND
        none was found                                 -> FAIL_DUPLICATE
    stage DONE (the post is recorded as published)     -> NOTHING

A lookup made before a send does not count after it: a send refused as a duplicate is looked
up again, since the post an earlier send published may not have been listed yet at the first
lookup. A lookup reads only posts published since the send started (less ``LOOKUP_MARGIN`` for
the clocks), so an older post of the same text, written by a person, is never taken for this
one.

This module is a port, plain data and a pure rule. It sends nothing and stores nothing; tokens
are opened by ``connection.account_tokens`` for the one caller that publishes, and none of
them is in a ``repr``.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    # Only the port's signatures name them: ``connection.py`` reaches the database, and the
    # writer's declarations, which read this module, must not load it.
    from openmarketer_core.social_accounts.connection import AccountTokens, PlatformApp

# What a platform's identifier of a post and the address of a post may be, at most.
MAX_EXTERNAL_ID_LENGTH = 64
MAX_EXTERNAL_URL_LENGTH = 2048
# How much earlier than the recorded start of a send a lookup reads, for the two clocks.
LOOKUP_MARGIN = timedelta(minutes=5)


class RefusalReason(StrEnum):
    """Why a platform refused a post, as a category a person can act on, never its words."""

    DUPLICATE = "duplicate"  # the account published this text already
    TOO_LONG = "too_long"
    MEDIA_REFUSED = "media_refused"  # the image was refused: its type, its size or its content
    NOT_PERMITTED = "not_permitted"  # the app or the account may not post (permissions, a lock)
    OUT_OF_CREDITS = "out_of_credits"  # the installation's developer account has no credit left
    REFUSED = "refused"  # anything else the platform refused


# What a person is told of each reason.
REFUSAL_SENTENCES = {
    RefusalReason.DUPLICATE: "the account already published this text",
    RefusalReason.TOO_LONG: "the platform says the text is too long",
    RefusalReason.MEDIA_REFUSED: "the platform refused the image",
    RefusalReason.NOT_PERMITTED: (
        "the platform does not let the app post as this account: check the app's permissions"
    ),
    RefusalReason.OUT_OF_CREDITS: "the installation's developer account has no credit left",
    RefusalReason.REFUSED: "the platform refused the post",
}


class PublishingFailure(StrEnum):
    """Why a post whose send started was not published, as a content item records it."""

    ACCESS_REVOKED = "access_revoked"
    DUPLICATE = RefusalReason.DUPLICATE.value
    TOO_LONG = RefusalReason.TOO_LONG.value
    MEDIA_REFUSED = RefusalReason.MEDIA_REFUSED.value
    NOT_PERMITTED = RefusalReason.NOT_PERMITTED.value
    OUT_OF_CREDITS = RefusalReason.OUT_OF_CREDITS.value
    REFUSED = RefusalReason.REFUSED.value

    @classmethod
    def of_refusal(cls, reason: RefusalReason) -> PublishingFailure:
        """The failure a platform's refusal for ``reason`` is recorded as."""
        return cls(reason.value)


class PublishingAccessRevoked(Exception):
    """The platform no longer accepts the account's token: connect the account again."""

    def __init__(self, platform_name: str) -> None:
        super().__init__(
            f"{platform_name} no longer accepts this account's access: connect the account again"
        )


class PublishingRefused(Exception):
    """The platform refused the post; nothing was published. ``reason`` says why."""

    def __init__(self, platform_name: str, reason: RefusalReason) -> None:
        super().__init__(f"{platform_name} refused the post: {REFUSAL_SENTENCES[reason]}")
        self.reason = reason

    @property
    def duplicate(self) -> bool:
        """Whether the refusal says the account published this text already."""
        return self.reason is RefusalReason.DUPLICATE


class PublishingUnavailable(Exception):
    """The platform could not be reached, failed or limits the rate; try again later.

    ``retry_after_seconds`` is how long the platform asked to wait, when it said.
    """

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class PublishingAccount:
    """The account a post is published from: the platform's identifier and its username.

    Public data: what a person approves a post to be published from. No token.
    """

    account_id: str
    username: str


@dataclass(frozen=True)
class PostImageFile:
    """The image a post is published with: its bytes and their media type (``image/png``)."""

    data: bytes = field(repr=False)
    media_type: str


@dataclass(frozen=True)
class PublishedPost:
    """A post on the platform: its identifier there and its public address."""

    external_id: str
    url: str


class PostPublisher(Protocol):
    """One platform's publishing: what its adapter does for a post."""

    def publish(
        self,
        app: PlatformApp,
        tokens: AccountTokens,
        account: PublishingAccount,
        *,
        text: str,
        image: PostImageFile | None,
    ) -> PublishedPost:
        """Publish ``text``, with ``image`` when there is one, as ``account``.

        Raises ``PublishingAccessRevoked``, ``PublishingRefused`` or ``PublishingUnavailable``.
        """
        ...

    def find_published(
        self,
        app: PlatformApp,
        tokens: AccountTokens,
        account: PublishingAccount,
        *,
        text: str,
        since: datetime,
    ) -> PublishedPost | None:
        """The account's post with ``text``, among its latest published since ``since``.

        ``None`` when there is none. Raises ``PublishingAccessRevoked``,
        ``PublishingRefused`` or ``PublishingUnavailable``.
        """
        ...


class PublishingStage(StrEnum):
    """Where a post was when an attempt to publish it began (``db/content_items.py``)."""

    STARTED = "started"  # nothing was sent before: this attempt started the send
    RESUMED = "resumed"  # an earlier attempt started a send whose outcome is not known
    DONE = "done"  # the post is recorded as published


class PublishingStep(StrEnum):
    """What the code that publishes does next."""

    SEND = "send"
    LOOK_UP = "look_up"
    RECORD_FOUND = "record_found"
    FAIL_DUPLICATE = "fail_duplicate"
    NOTHING = "nothing"


@dataclass(frozen=True)
class PublishingSituation:
    """What an attempt knows: the stage it began at, whether it looked the post up and what
    it found, and whether the platform refused its send as a duplicate.

    After a send, the situation starts again from the stage and the refusal: a lookup made
    before the send says nothing of what the send did.
    """

    stage: PublishingStage
    looked_up: bool = False
    found: PublishedPost | None = None
    refused_as_duplicate: bool = False


def next_publishing_step(situation: PublishingSituation) -> PublishingStep:
    """What to do next so that a post is published once, whatever earlier attempts did."""
    if situation.stage is PublishingStage.DONE:
        return PublishingStep.NOTHING
    if situation.refused_as_duplicate or situation.stage is PublishingStage.RESUMED:
        if not situation.looked_up:
            return PublishingStep.LOOK_UP
        if situation.found is not None:
            return PublishingStep.RECORD_FOUND
        if situation.refused_as_duplicate:
            return PublishingStep.FAIL_DUPLICATE
    return PublishingStep.SEND


def lookup_since(started_at: datetime) -> datetime:
    """From when a lookup reads the account's posts, for a send that started at
    ``started_at``."""
    return started_at - LOOKUP_MARGIN


def text_key(text: str) -> str:
    """``text`` as two posts are compared for being the same: its words, one space apart.

    Leading, trailing and repeated whitespace (line breaks included) and the Unicode form of
    a character (platforms store text composed, NFC) do not make two texts different; case
    and punctuation do.
    """
    return " ".join(unicodedata.normalize("NFC", text).split())


def text_fingerprint(text: str) -> str:
    """The SHA-256 of ``text_key(text)``, in hex: what two posts are compared by in a store."""
    return hashlib.sha256(text_key(text).encode("utf-8")).hexdigest()
