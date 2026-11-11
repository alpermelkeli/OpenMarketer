"""Publishing a post from a connected X account through X API v2, over httpx.

``XPublisher`` is the ``PostPublisher`` of X. ``publish`` uploads the post's image, when it
has one, with the simple upload (``POST 2/media/upload``, multipart: ``media`` and
``media_category=tweet_image``), then creates the post (``POST 2/tweets``, JSON: ``text`` and
``media.media_ids``). ``find_published`` reads the account's five latest posts since a time,
without replies or reposts (``GET 2/users/{id}/tweets``), and finds the one with the text.
Every request is signed with OAuth 1.0a as the account (``oauth1.py``); neither a multipart nor
a JSON body is covered by the signature (RFC 5849, 3.4.1.3.1: only a form body's parameters
are), so only the query's parameters and the ``oauth_*`` ones are signed.

X's documentation:

- https://docs.x.com/x-api/media/upload-media
- https://docs.x.com/x-api/posts/create-post
- https://docs.x.com/x-api/users/get-posts
- https://docs.x.com/resources/fundamentals/authentication/oauth-1-0a/creating-a-signature

Every request goes to a fixed HTTPS URL of ``api.x.com`` (the account's identifier, digits,
is the only part of a path that varies), with a timeout and without following a redirect.
What X answers when it refuses is logged, shortened, and never put in an error: the answer is
read only to choose a ``RefusalReason`` (a 403 whose problem says "duplicate content", or the
v1.1 code 187, is a duplicate; code 186 or "too long" is a text too long; a refused upload is
a refused image; 402 is a developer account without credit; any other 403 is not permitted).
401 means the account's token is no longer accepted; 429 and 5xx, and a request whose answer
never came, are unavailable, with how long X asked to wait when it said
(``x-rate-limit-reset``, ``retry-after``).

What X answers about a post is data: its identifier must be digits, and the address of a post
is built here (``https://x.com/{username}/status/{id}``), never taken from X. The text X shows
of a post escapes ``&``, ``<`` and ``>`` and ends with the address of its image when it has
one, so a lookup compares texts without those (``same_text``).

This adapter stores nothing and opens no token: it is given them.
"""

from __future__ import annotations

import html
import logging
import re
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeGuard
from urllib.parse import urlencode

import httpx

from openmarketer_core.social_accounts.connection import AccountTokens, PlatformApp
from openmarketer_core.social_accounts.oauth1 import (
    OAuth1Client,
    OAuth1Token,
    authorization_header,
)
from openmarketer_core.social_accounts.publishing import (
    MAX_EXTERNAL_ID_LENGTH,
    PostImageFile,
    PublishedPost,
    PublishingAccessRevoked,
    PublishingAccount,
    PublishingRefused,
    PublishingUnavailable,
    RefusalReason,
    text_key,
)

logger = logging.getLogger(__name__)

PLATFORM_NAME = "X"
MEDIA_UPLOAD_URL = "https://api.x.com/2/media/upload"
CREATE_POST_URL = "https://api.x.com/2/tweets"
USER_POSTS_URL = "https://api.x.com/2/users/{account_id}/tweets"
POST_URL = "https://x.com/{username}/status/{post_id}"
# How many of the account's latest posts a lookup reads: X's least.
LOOKUP_POSTS = 5
# X's simple upload takes an image of these types up to 5 MB.
IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
MAX_IMAGE_BYTES = 5 * 1024 * 1024
TIMEOUT_S = 30.0
_LOGGED_CHARACTERS = 300
_DIGITS = re.compile(r"[0-9]+")
_USERNAME = re.compile(r"[A-Za-z0-9_]+")
# X ends the text of a post with an image with the image's short address.
_TRAILING_MEDIA_LINK = re.compile(r"(\s+https://t\.co/[A-Za-z0-9]+)+\s*$")
# X's v1.1 codes, which some answers still carry: a duplicate, a text too long.
_DUPLICATE_CODE = 187
_TOO_LONG_CODE = 186


def _new_nonce() -> str:
    return secrets.token_hex(16)


def _now() -> int:
    return int(time.time())


@dataclass(frozen=True)
class XPublisher:
    """X's publishing. ``transport``, ``nonce`` and ``clock`` are for tests."""

    transport: httpx.BaseTransport | None = None
    nonce: Callable[[], str] = _new_nonce
    clock: Callable[[], int] = _now

    def publish(
        self,
        app: PlatformApp,
        tokens: AccountTokens,
        account: PublishingAccount,
        *,
        text: str,
        image: PostImageFile | None,
    ) -> PublishedPost:
        """Publish ``text``, with ``image`` when there is one, as ``account``."""
        post_url_parts = _account_parts(account)
        body: dict[str, Any] = {"text": text}
        if image is not None:
            body["media"] = {"media_ids": [self._upload(app, tokens, image)]}
        response = self._send("POST", CREATE_POST_URL, app=app, tokens=tokens, json=body)
        _refuse_unless("post", response, ok=(200, 201), now=self.clock())
        data = _data_of("post", response)
        if not isinstance(data, dict):
            raise PublishingUnavailable("X answered the post without it")
        return _published(data.get("id"), post_url_parts)

    def find_published(
        self,
        app: PlatformApp,
        tokens: AccountTokens,
        account: PublishingAccount,
        *,
        text: str,
        since: datetime,
    ) -> PublishedPost | None:
        """The account's post with ``text`` among its latest published since ``since``."""
        username, account_id = _account_parts(account)
        query = [
            ("max_results", str(LOOKUP_POSTS)),
            ("exclude", "replies,retweets"),
            ("start_time", _x_time(since)),
        ]
        response = self._send(
            "GET",
            USER_POSTS_URL.format(account_id=account_id),
            app=app,
            tokens=tokens,
            query=query,
        )
        _refuse_unless("lookup", response, ok=(200,), now=self.clock())
        data = _data_of("lookup", response)
        if data is None:
            return None  # X leaves ``data`` out when the account has no such post
        if not isinstance(data, list):
            raise PublishingUnavailable("X answered the lookup with no list of posts")
        for post in data:
            if isinstance(post, dict) and isinstance(post.get("text"), str):
                if same_text(text, post["text"]):
                    return _published(post.get("id"), (username, account_id))
        return None

    def _upload(self, app: PlatformApp, tokens: AccountTokens, image: PostImageFile) -> str:
        if image.media_type not in IMAGE_TYPES or len(image.data) > MAX_IMAGE_BYTES:
            raise PublishingRefused(PLATFORM_NAME, RefusalReason.MEDIA_REFUSED)
        response = self._send(
            "POST",
            MEDIA_UPLOAD_URL,
            app=app,
            tokens=tokens,
            files={"media": ("image", image.data, image.media_type)},
            form={"media_category": "tweet_image"},
        )
        _refuse_unless(
            "image upload",
            response,
            ok=(200, 201),
            now=self.clock(),
            refused=RefusalReason.MEDIA_REFUSED,
        )
        data = _data_of("image upload", response)
        media_id = data.get("id") if isinstance(data, dict) else None
        if not _is_digits(media_id):
            raise PublishingUnavailable("X answered the image upload without a usable id")
        return media_id

    def _send(
        self,
        method: str,
        url: str,
        *,
        app: PlatformApp,
        tokens: AccountTokens,
        query: Sequence[tuple[str, str]] = (),
        json: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        form: dict[str, str] | None = None,
    ) -> httpx.Response:
        signed_url = f"{url}?{urlencode(query)}" if query else url
        # Neither a JSON nor a multipart body is signed: only the query and the oauth_* ones.
        header = authorization_header(
            method,
            signed_url,
            client=OAuth1Client(key=app.key, secret=app.secret),
            token=OAuth1Token(token=tokens.access_token, secret=tokens.access_secret),
            nonce=self.nonce(),
            timestamp=self.clock(),
        )
        try:
            with httpx.Client(
                timeout=TIMEOUT_S, transport=self.transport, follow_redirects=False
            ) as client:
                return client.request(
                    method,
                    signed_url,
                    headers={"Authorization": header},
                    json=json,
                    files=files,
                    data=form,
                )
        except httpx.HTTPError as e:
            # The type only: the exception's text can carry the request's URL or headers.
            raise PublishingUnavailable(f"X could not be reached: {type(e).__name__}") from None


def same_text(sent: str, shown: str) -> bool:
    """Whether ``shown``, a post's text as X shows it, is the text ``sent``.

    X escapes ``&``, ``<`` and ``>`` and ends the text of a post with an image with the image's
    short address; whitespace and the Unicode form do not count (``text_key``).
    """
    unescaped = html.unescape(_TRAILING_MEDIA_LINK.sub("", shown))
    return text_key(unescaped) == text_key(sent)


def _account_parts(account: PublishingAccount) -> tuple[str, str]:
    """The account's username and identifier, refused unless they are X's characters."""
    if not _USERNAME.fullmatch(account.username) or not _is_digits(account.account_id):
        raise PublishingRefused(PLATFORM_NAME, RefusalReason.REFUSED)
    return account.username, account.account_id


def _published(post_id: object, account: tuple[str, str]) -> PublishedPost:
    if not _is_digits(post_id):
        raise PublishingUnavailable("X answered a post without a usable id")
    username, _ = account
    return PublishedPost(
        external_id=post_id, url=POST_URL.format(username=username, post_id=post_id)
    )


def _is_digits(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) <= MAX_EXTERNAL_ID_LENGTH
        and bool(_DIGITS.fullmatch(value))
    )


def _x_time(moment: datetime) -> str:
    """``moment`` as X reads a time: UTC, to the second, with a ``Z``."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _data_of(step: str, response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError:
        raise PublishingUnavailable(f"X answered the {step} with no JSON") from None
    return body.get("data") if isinstance(body, dict) else None


def _refuse_unless(
    step: str,
    response: httpx.Response,
    *,
    ok: tuple[int, ...],
    now: int,
    refused: RefusalReason | None = None,
) -> None:
    """Raise what an answer that is not ``ok`` means; ``refused`` is what any refusal of this
    step is, when the step decides it (an image upload's refusal is a refused image), and
    ``now`` the time in seconds since the epoch, for how long X asked to wait."""
    status = response.status_code
    if status in ok:
        return
    logger.warning(
        "X answered the %s with HTTP %s: %r", step, status, response.text[:_LOGGED_CHARACTERS]
    )
    if status == 401:
        raise PublishingAccessRevoked(PLATFORM_NAME)
    if status == 429 or status >= 500:
        raise PublishingUnavailable(
            f"X answered the {step} with HTTP {status}",
            retry_after_seconds=_retry_after(response, now),
        )
    if 400 <= status < 500:
        raise PublishingRefused(PLATFORM_NAME, refused or _refusal_reason(response))
    raise PublishingUnavailable(f"X answered the {step} with HTTP {status}")


def _refusal_reason(response: httpx.Response) -> RefusalReason:
    """The category of X's refusal, read from its answer; its words go nowhere else."""
    if response.status_code == 402:
        return RefusalReason.OUT_OF_CREDITS
    words, codes = _problem_of(response)
    if _DUPLICATE_CODE in codes or "duplicate content" in words:
        return RefusalReason.DUPLICATE
    if _TOO_LONG_CODE in codes or "too long" in words:
        return RefusalReason.TOO_LONG
    if response.status_code == 403:
        return RefusalReason.NOT_PERMITTED
    return RefusalReason.REFUSED


def _problem_of(response: httpx.Response) -> tuple[str, set[int]]:
    """The words of X's problem (its title, detail and messages) in lower case, and its codes.

    X answers a refusal with a problem (``title``, ``detail``, ``type``), with ``errors``
    holding problems or v1.1 errors (``code``, ``message``); anything else reads as nothing.
    """
    try:
        body = response.json()
    except ValueError:
        return "", set()
    problems = [body] if isinstance(body, dict) else []
    if isinstance(body, dict) and isinstance(body.get("errors"), list):
        problems += [error for error in body["errors"] if isinstance(error, dict)]
    words: list[str] = []
    codes: set[int] = set()
    for problem in problems:
        for name in ("title", "detail", "message", "type"):
            value = problem.get(name)
            if isinstance(value, str):
                words.append(value.lower())
        code = problem.get("code")
        if isinstance(code, int):
            codes.add(code)
    return " ".join(words), codes


def _retry_after(response: httpx.Response, now: int) -> float | None:
    """How long X asked to wait, in seconds, when it said: ``retry-after`` or the time the
    rate limit resets (``x-rate-limit-reset``, seconds since the epoch)."""
    retry_after = response.headers.get("retry-after", "")
    if retry_after.isdigit():
        return float(retry_after)
    reset = response.headers.get("x-rate-limit-reset", "")
    if reset.isdigit():
        return max(0.0, float(int(reset) - now))
    return None
