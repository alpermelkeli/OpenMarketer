"""Connecting a project's account on a social platform, listing and disconnecting it.

Connecting takes two requests of the person's browser. ``start_connection`` asks the
platform, through its adapter, where to send the person, and keeps an attempt named by the
``state`` the platform will answer with, with the secret the answer needs. The person agrees
on the platform, which sends them back to the callback URL with that state and a grant (with
OAuth 1.0a, the ``oauth_token`` and the ``oauth_verifier``); ``finish_connection`` takes the
attempt, which is used once, has the adapter exchange the grant for the account's token, and
stores the account. ``abandon_connection`` forgets an attempt the person turned down. Both
are given the platform the answer came from, and an attempt started on another platform is
not found. ``connectable_platforms`` says which platforms can be connected now: those whose
app has its credentials, set or stored.

A project has one account per platform. Connecting the account it has again updates it;
connecting another replaces it, and the replaced account's token is forgotten. One account
may be connected to several projects.

The port each platform implements is ``AccountAuthorization``: a new platform brings one
adapter in its own folder and an entry in ``catalog.py``, and nothing here changes. An
adapter raises ``AuthorizationRefused`` or ``AuthorizationUnavailable``, whose messages say
what failed and never repeat what the platform answered.

Secrets are sealed with the installation's ``CredentialCipher``, bound to the workspace and
to what they belong to (``_TOKEN_BINDING``, ``_ATTEMPT_BINDING``), so a row copied to another
project or attempt does not open. No secret is in a returned account, an error or a
``repr``; the one function that opens an account's tokens is ``account_tokens``, for the code
that will publish with them.

Every function receives what it needs: the session, the environment as a mapping (the app's
credentials may be set there or stored), the workspace, the cipher (``None`` when the
installation has no ``ENCRYPTION_KEY``) and the adapter. None of them commits. Disconnecting
forgets the token; it does not revoke it on the platform, where the person can remove the
app's access themselves.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from openmarketer_core.credentials.catalog import Credential, SealedValue
from openmarketer_core.credentials.cipher import CredentialCipher, SealedValueDoesNotOpen
from openmarketer_core.credentials.resolution import (
    CredentialSource,
    EncryptionKeyMissing,
    credential_statuses,
    credential_value,
)
from openmarketer_core.db.projects import get_project
from openmarketer_core.db.social_accounts import (
    ConnectedAccount,
    SealedAccountTokens,
    connected_accounts,
    remove_connected_account,
    sealed_account_tokens,
    store_connected_account,
)
from openmarketer_core.db.social_connection_attempts import (
    store_connection_attempt,
    take_connection_attempt,
)
from openmarketer_core.social_accounts.catalog import (
    MAX_STATE_LENGTH,
    PLATFORMS,
    AccountDescription,
    ConnectablePlatform,
    SocialPlatform,
    connectable_platform,
)

# Long enough to read the platform's page and agree; a request token is not kept longer.
ATTEMPT_LIFETIME = timedelta(minutes=15)

_TOKEN_BINDING = "social_account:{project_id}:{platform}:{part}"
_ATTEMPT_BINDING = "social_connection_attempt:{project_id}:{platform}:{state}"


class AuthorizationRefused(Exception):
    """The platform refused a step of the authorization; the message says which, never why."""


class AuthorizationUnavailable(Exception):
    """The platform could not be reached, or answered something that is not an answer."""


class PlatformAppNotSet(Exception):
    """A credential of the installation's app on the platform is neither set nor stored."""

    def __init__(self, platform: ConnectablePlatform, credential: Credential) -> None:
        super().__init__(
            f"connecting an account on {platform.name} needs the installation's app:"
            f" set {credential.name} in the dashboard's Settings, or with the environment"
            f" variable {credential.environment_variable}"
        )
        self.credential = credential


class CallbackUrlRefused(Exception):
    """The callback URL is not an absolute http or https URL."""

    def __init__(self) -> None:
        super().__init__("the callback URL must be an absolute http or https URL")


class ConnectionAttemptNotFound(Exception):
    """No attempt of the workspace has that state: it was never started, or was used already."""

    def __init__(self) -> None:
        super().__init__(
            "this connection was not started here, or was finished already: connect again"
        )


class ConnectionAttemptExpired(Exception):
    """The attempt is older than ``ATTEMPT_LIFETIME``."""

    def __init__(self) -> None:
        minutes = int(ATTEMPT_LIFETIME.total_seconds() // 60)
        super().__init__(
            f"this connection was started more than {minutes} minutes ago: connect again"
        )


class ConnectionSecretUnreadable(Exception):
    """A secret this installation sealed does not open: ``ENCRYPTION_KEY`` changed meanwhile."""

    def __init__(self) -> None:
        super().__init__(
            "a stored secret of this connection cannot be read: set ENCRYPTION_KEY to the key"
            " it was stored with, or connect the account again"
        )


@dataclass(frozen=True)
class PlatformApp:
    """The installation's app on a platform: its key and its secret (OAuth 1.0a's consumer
    key and secret, OAuth 2's client id and secret)."""

    key: str = field(repr=False)
    secret: str = field(repr=False)


@dataclass(frozen=True)
class AuthorizationStart:
    """Where to send the person, the state the platform will answer with, and the secret the
    answer will need (OAuth 1.0a's request token secret)."""

    authorize_url: str
    state: str
    secret_to_keep: str = field(repr=False)


@dataclass(frozen=True)
class AuthorizedAccount:
    """The account a person authorized, as the platform describes it, and its token."""

    account: AccountDescription
    access_token: str = field(repr=False)
    access_secret: str = field(repr=False)


@dataclass(frozen=True)
class AccountTokens:
    """An account's token and its secret, opened, for the code that acts as the account."""

    access_token: str = field(repr=False)
    access_secret: str = field(repr=False)


@dataclass(frozen=True)
class PlatformReadiness:
    """A platform of the catalog and the credentials of its app that are neither set nor stored."""

    platform: ConnectablePlatform
    missing: tuple[Credential, ...]

    @property
    def ready(self) -> bool:
        """Whether the app has every credential, so that an account can be connected."""
        return not self.missing


class AccountAuthorization(Protocol):
    """One platform's authorization flow: what its adapter does for a connection."""

    def start(self, app: PlatformApp, *, callback_url: str) -> AuthorizationStart:
        """Ask the platform for a new authorization that comes back to ``callback_url``."""
        ...

    def finish(
        self, app: PlatformApp, *, state: str, grant: str, kept_secret: str
    ) -> AuthorizedAccount:
        """Exchange the grant the callback brought for the account and its token."""
        ...


def start_connection(
    session: Session,
    environ: Mapping[str, str],
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    platform: SocialPlatform,
    callback_url: str,
    authorization: AccountAuthorization,
    cipher: CredentialCipher | None,
) -> str:
    """Start connecting the project's account on ``platform``; the URL to send the person to.

    Raises ``ProjectNotFound``, ``CallbackUrlRefused``, ``EncryptionKeyMissing``,
    ``PlatformAppNotSet``, ``CredentialUnreadable`` (a stored app credential that does not
    open), and what the adapter raises: ``AuthorizationRefused`` (often a wrong app key or a
    callback URL the app does not allow) and ``AuthorizationUnavailable``.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    _check_callback_url(callback_url)
    if cipher is None:
        raise EncryptionKeyMissing
    connectable = connectable_platform(platform.value)
    app = _platform_app(session, environ, workspace_id, connectable, cipher)
    started = authorization.start(app, callback_url=callback_url)
    if not 1 <= len(started.state) <= MAX_STATE_LENGTH:
        raise AuthorizationUnavailable(f"{connectable.name} named the connection unusably")
    binding = _ATTEMPT_BINDING.format(
        project_id=project_id, platform=platform.value, state=started.state
    )
    store_connection_attempt(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        platform=platform,
        state=started.state,
        secret=cipher.seal(
            started.secret_to_keep, workspace_id=workspace_id, credential_name=binding
        ),
        lifetime=ATTEMPT_LIFETIME,
    )
    return started.authorize_url


def finish_connection(
    session: Session,
    environ: Mapping[str, str],
    *,
    workspace_id: uuid.UUID,
    platform: SocialPlatform,
    state: str,
    grant: str,
    connected_by: uuid.UUID,
    authorization: AccountAuthorization,
    cipher: CredentialCipher | None,
) -> ConnectedAccount:
    """Finish the attempt on ``platform`` named ``state`` with the platform's ``grant``; the
    account connected.

    ``authorization`` is the adapter of ``platform``. An attempt started on another platform
    is not found, and stays. Raises ``EncryptionKeyMissing``, ``ConnectionAttemptNotFound``,
    ``ConnectionAttemptExpired``, ``ConnectionSecretUnreadable``, ``PlatformAppNotSet``,
    ``CredentialUnreadable``, ``AuthorizationRefused`` and ``AuthorizationUnavailable``.
    """
    if cipher is None:
        raise EncryptionKeyMissing
    attempt = take_connection_attempt(
        session, workspace_id=workspace_id, platform=platform, state=state
    )
    if attempt is None:
        raise ConnectionAttemptNotFound
    if attempt.expired:
        raise ConnectionAttemptExpired
    binding = _ATTEMPT_BINDING.format(
        project_id=attempt.project_id, platform=attempt.platform.value, state=state
    )
    kept_secret = _opened(cipher, attempt.secret, workspace_id, binding)
    connectable = connectable_platform(attempt.platform.value)
    app = _platform_app(session, environ, workspace_id, connectable, cipher)
    authorized = authorization.finish(app, state=state, grant=grant, kept_secret=kept_secret)
    return store_connected_account(
        session,
        workspace_id=workspace_id,
        project_id=attempt.project_id,
        platform=attempt.platform,
        account=authorized.account,
        tokens=_sealed_tokens(
            cipher, authorized, workspace_id, attempt.project_id, attempt.platform
        ),
        connected_by=connected_by,
    )


def abandon_connection(
    session: Session, *, workspace_id: uuid.UUID, platform: SocialPlatform, state: str
) -> uuid.UUID | None:
    """Forget the attempt on ``platform`` named ``state``, which the person turned down.

    The project the attempt was for, or ``None`` when there was no such attempt (an attempt
    started on another platform is not one, and stays).
    """
    attempt = take_connection_attempt(
        session, workspace_id=workspace_id, platform=platform, state=state
    )
    return None if attempt is None else attempt.project_id


def connectable_platforms(
    session: Session, environ: Mapping[str, str], *, workspace_id: uuid.UUID
) -> list[PlatformReadiness]:
    """Every platform of the catalog, in its order, with the app credentials it still needs.

    A credential counts as there when the environment sets it or the workspace stored it;
    nothing is decrypted, so a stored value that no longer opens still counts.
    """
    unset = {
        status.credential
        for status in credential_statuses(session, environ, workspace_id=workspace_id)
        if status.source is CredentialSource.NOT_SET
    }
    return [
        PlatformReadiness(
            platform=connectable,
            missing=tuple(
                credential
                for credential in (connectable.app_key, connectable.app_secret)
                if credential in unset
            ),
        )
        for connectable in PLATFORMS
    ]


def list_accounts(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> list[ConnectedAccount]:
    """The project's connected accounts, one per platform at most, by platform.

    Raises ``ProjectNotFound``.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    return connected_accounts(session, workspace_id=workspace_id, project_id=project_id)


def disconnect(
    session: Session, *, workspace_id: uuid.UUID, project_id: uuid.UUID, platform: SocialPlatform
) -> bool:
    """Forget the project's account on ``platform`` and its token; whether there was one.

    Raises ``ProjectNotFound``. The token is not revoked on the platform.
    """
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    return remove_connected_account(
        session, workspace_id=workspace_id, project_id=project_id, platform=platform
    )


def account_tokens(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    platform: SocialPlatform,
    cipher: CredentialCipher | None,
) -> AccountTokens | None:
    """The opened token of the project's account on ``platform``, or ``None`` when none is
    connected: for the code that acts as the account, and nothing that shows it.

    Raises ``EncryptionKeyMissing`` and ``ConnectionSecretUnreadable``.
    """
    sealed = sealed_account_tokens(
        session, workspace_id=workspace_id, project_id=project_id, platform=platform
    )
    if sealed is None:
        return None
    if cipher is None:
        raise EncryptionKeyMissing
    return AccountTokens(
        access_token=_opened(
            cipher, sealed.access_token, workspace_id, _token_binding(project_id, platform, "token")
        ),
        access_secret=_opened(
            cipher,
            sealed.access_secret,
            workspace_id,
            _token_binding(project_id, platform, "secret"),
        ),
    )


def _check_callback_url(callback_url: str) -> None:
    parts = urlsplit(callback_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CallbackUrlRefused


def _platform_app(
    session: Session,
    environ: Mapping[str, str],
    workspace_id: uuid.UUID,
    connectable: ConnectablePlatform,
    cipher: CredentialCipher,
) -> PlatformApp:
    values: list[str] = []
    for credential in (connectable.app_key, connectable.app_secret):
        value = credential_value(
            session, environ, workspace_id=workspace_id, credential=credential, cipher=cipher
        )
        if value is None:
            raise PlatformAppNotSet(connectable, credential)
        values.append(value)
    key, secret = values
    return PlatformApp(key=key, secret=secret)


def _sealed_tokens(
    cipher: CredentialCipher,
    authorized: AuthorizedAccount,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    platform: SocialPlatform,
) -> SealedAccountTokens:
    return SealedAccountTokens(
        access_token=cipher.seal(
            authorized.access_token,
            workspace_id=workspace_id,
            credential_name=_token_binding(project_id, platform, "token"),
        ),
        access_secret=cipher.seal(
            authorized.access_secret,
            workspace_id=workspace_id,
            credential_name=_token_binding(project_id, platform, "secret"),
        ),
    )


def _token_binding(project_id: uuid.UUID, platform: SocialPlatform, part: str) -> str:
    return _TOKEN_BINDING.format(project_id=project_id, platform=platform.value, part=part)


def _opened(
    cipher: CredentialCipher, sealed: SealedValue, workspace_id: uuid.UUID, binding: str
) -> str:
    try:
        return cipher.open(sealed, workspace_id=workspace_id, credential_name=binding)
    except SealedValueDoesNotOpen:
        raise ConnectionSecretUnreadable from None
