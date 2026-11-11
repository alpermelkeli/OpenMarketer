"""Connecting an X account through X's three-legged OAuth 1.0a flow, over httpx.

``XAuthorization`` is the ``AccountAuthorization`` of X. ``start`` asks X for a request token
that comes back to the callback URL, with write access, and answers the page of X where the
person agrees (``oauth/authenticate``); the request token is the attempt's state and its
secret is kept for the answer. ``finish`` exchanges the request token and the verifier X
sent back for the account's access token and secret, then reads the account with them
(``2/users/me``). X's OAuth 1.0a tokens do not expire, so there is nothing to refresh.

Every request goes to a fixed HTTPS URL of ``api.x.com``, with a timeout and without
following a redirect. What X answers when it refuses is logged, shortened, and never put in
an error, which says the step and the HTTP status only; an answer that succeeded carries
tokens and is not logged. What X says about the account is data: the identifier must be
digits, the username X's characters, and everything is kept within the catalog's bounds.

This adapter does not store anything, does not post and does not revoke a token.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeGuard
from urllib.parse import parse_qsl, urlencode

import httpx

from openmarketer_core.social_accounts.catalog import (
    MAX_ACCOUNT_ID_LENGTH,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_PICTURE_URL_LENGTH,
    MAX_USERNAME_LENGTH,
    AccountDescription,
)
from openmarketer_core.social_accounts.connection import (
    AuthorizationRefused,
    AuthorizationStart,
    AuthorizationUnavailable,
    AuthorizedAccount,
    PlatformApp,
)
from openmarketer_core.social_accounts.oauth1 import (
    OAuth1Client,
    OAuth1Token,
    authorization_header,
)

logger = logging.getLogger(__name__)

REQUEST_TOKEN_URL = "https://api.x.com/oauth/request_token"
AUTHENTICATE_URL = "https://api.x.com/oauth/authenticate"
ACCESS_TOKEN_URL = "https://api.x.com/oauth/access_token"
USERS_ME_URL = "https://api.x.com/2/users/me"
# The fields of the account a person is shown when it is connected.
USER_FIELDS = "profile_image_url,name,username"
TIMEOUT_S = 15.0
# What of an answer X refused goes to the log.
_LOGGED_CHARACTERS = 300
_ACCOUNT_ID = re.compile(r"[0-9]+")
_USERNAME = re.compile(r"[A-Za-z0-9_]+")
# Anything a person is shown must not move the cursor or hide text.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")


def _new_nonce() -> str:
    return secrets.token_hex(16)


def _now() -> int:
    return int(time.time())


@dataclass(frozen=True)
class XAuthorization:
    """X's authorization flow. ``transport``, ``nonce`` and ``clock`` are for tests."""

    transport: httpx.BaseTransport | None = None
    nonce: Callable[[], str] = _new_nonce
    clock: Callable[[], int] = _now

    def start(self, app: PlatformApp, *, callback_url: str) -> AuthorizationStart:
        """A request token for ``callback_url`` with write access, and X's page to agree on."""
        form = [("x_auth_access_type", "write")]
        answer = self._form_answer(
            "request token",
            self._send(
                "POST",
                REQUEST_TOKEN_URL,
                app=app,
                token=None,
                form=form,
                oauth_parameters={"oauth_callback": callback_url},
            ),
        )
        if answer.get("oauth_callback_confirmed") != "true":
            raise AuthorizationUnavailable("X did not confirm the callback of the request token")
        token = _field(answer, "oauth_token", "request token")
        secret = _field(answer, "oauth_token_secret", "request token")
        return AuthorizationStart(
            authorize_url=f"{AUTHENTICATE_URL}?{urlencode({'oauth_token': token})}",
            state=token,
            secret_to_keep=secret,
        )

    def finish(
        self, app: PlatformApp, *, state: str, grant: str, kept_secret: str
    ) -> AuthorizedAccount:
        """The account and its access token, for the request token ``state`` and its verifier."""
        answer = self._form_answer(
            "access token",
            self._send(
                "POST",
                ACCESS_TOKEN_URL,
                app=app,
                token=OAuth1Token(token=state, secret=kept_secret),
                oauth_parameters={"oauth_verifier": grant},
            ),
        )
        access = OAuth1Token(
            token=_field(answer, "oauth_token", "access token"),
            secret=_field(answer, "oauth_token_secret", "access token"),
        )
        account = self._account(app, access)
        if "user_id" in answer and answer["user_id"] != account.account_id:
            raise AuthorizationUnavailable("X answered the access token of another account")
        return AuthorizedAccount(
            account=account, access_token=access.token, access_secret=access.secret
        )

    def _account(self, app: PlatformApp, access: OAuth1Token) -> AccountDescription:
        response = self._send(
            "GET", USERS_ME_URL, app=app, token=access, query=[("user.fields", USER_FIELDS)]
        )
        _refuse_unless_ok("account", response)
        try:
            body = response.json()
        except ValueError:
            raise AuthorizationUnavailable("X answered the account with no JSON") from None
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise AuthorizationUnavailable("X answered no account")
        return _description(data)

    def _send(
        self,
        method: str,
        url: str,
        *,
        app: PlatformApp,
        token: OAuth1Token | None,
        form: Sequence[tuple[str, str]] = (),
        query: Sequence[tuple[str, str]] = (),
        oauth_parameters: dict[str, str] | None = None,
    ) -> httpx.Response:
        signed_url = f"{url}?{urlencode(query)}" if query else url
        header = authorization_header(
            method,
            signed_url,
            client=OAuth1Client(key=app.key, secret=app.secret),
            token=token,
            nonce=self.nonce(),
            timestamp=self.clock(),
            parameters=form,
            oauth_parameters=oauth_parameters,
        )
        try:
            with httpx.Client(
                timeout=TIMEOUT_S, transport=self.transport, follow_redirects=False
            ) as client:
                return client.request(
                    method,
                    signed_url,
                    headers={"Authorization": header},
                    data=dict(form) if form else None,
                )
        except httpx.HTTPError as e:
            # The type only: the exception's text can carry the request's URL or headers.
            raise AuthorizationUnavailable(f"X could not be reached: {type(e).__name__}") from None

    def _form_answer(self, step: str, response: httpx.Response) -> dict[str, str]:
        _refuse_unless_ok(step, response)
        return dict(parse_qsl(response.text, keep_blank_values=True))


def _refuse_unless_ok(step: str, response: httpx.Response) -> None:
    if response.status_code == 200:
        return
    logger.warning(
        "X answered the %s with HTTP %s: %r",
        step,
        response.status_code,
        response.text[:_LOGGED_CHARACTERS],
    )
    if 400 <= response.status_code < 500:
        raise AuthorizationRefused(f"X refused the {step}: HTTP {response.status_code}")
    raise AuthorizationUnavailable(f"X answered the {step} with HTTP {response.status_code}")


def _field(answer: dict[str, str], name: str, step: str) -> str:
    value = answer.get(name, "")
    if not value:
        raise AuthorizationUnavailable(f"X answered the {step} without {name}")
    return value


def _description(data: dict[str, Any]) -> AccountDescription:
    account_id = data.get("id")
    username = data.get("username")
    if not _matches(account_id, _ACCOUNT_ID, MAX_ACCOUNT_ID_LENGTH):
        raise AuthorizationUnavailable("X answered an account without a usable identifier")
    if not _matches(username, _USERNAME, MAX_USERNAME_LENGTH):
        raise AuthorizationUnavailable("X answered an account without a usable username")
    return AccountDescription(
        account_id=account_id,
        username=username,
        display_name=_display_name(data.get("name"), username),
        picture_url=_picture_url(data.get("profile_image_url")),
    )


def _matches(value: object, pattern: re.Pattern[str], max_length: int) -> TypeGuard[str]:
    return isinstance(value, str) and len(value) <= max_length and bool(pattern.fullmatch(value))


def _display_name(name: Any, username: str) -> str:
    if not isinstance(name, str):
        return username
    shown = _CONTROL.sub("", name).strip()[:MAX_DISPLAY_NAME_LENGTH]
    return shown or username


def _picture_url(url: Any) -> str | None:
    """X's picture of the account, only as an HTTPS URL within the bound; otherwise none."""
    if not isinstance(url, str) or len(url) > MAX_PICTURE_URL_LENGTH:
        return None
    if not url.startswith("https://") or _CONTROL.search(url) or " " in url:
        return None
    return url
