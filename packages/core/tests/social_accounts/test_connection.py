"""Tests for connecting, listing and disconnecting a project's social accounts.

They need PostgreSQL (see the root ``conftest.py``). The platform is a fake adapter of the
``AccountAuthorization`` port; keys are made up in the test.
"""

import base64
import os
import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from openmarketer_core.credentials.cipher import CredentialCipher
from openmarketer_core.credentials.resolution import EncryptionKeyMissing, set_credential
from openmarketer_core.db.models import (
    SocialAccountRecord,
    SocialConnectionAttemptRecord,
    Workspace,
)
from openmarketer_core.db.projects import ProjectNotFound, create_project
from openmarketer_core.social_accounts.catalog import AccountDescription, SocialPlatform
from openmarketer_core.social_accounts.connection import (
    AuthorizationStart,
    AuthorizedAccount,
    CallbackUrlRefused,
    ConnectionAttemptExpired,
    ConnectionAttemptNotFound,
    ConnectionSecretUnreadable,
    PlatformApp,
    PlatformAppNotSet,
    abandon_connection,
    account_tokens,
    disconnect,
    finish_connection,
    list_accounts,
    start_connection,
)

OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
CALLBACK = "http://localhost:8000/v1/social-accounts/x/callback"
APP_KEY = "app-key-0123"
APP_SECRET = "app-secret-0123"
ENVIRON = {"X_API_KEY": APP_KEY, "X_API_SECRET": APP_SECRET}
STATE = "request-token"
REQUEST_SECRET = "request-token-secret"
VERIFIER = "verifier"
ACCESS_TOKEN = "42-access-token-value"
ACCESS_SECRET = "access-secret-value"
ACME = AccountDescription(account_id="42", username="acme", display_name="Acme", picture_url=None)
SECRETS = (APP_SECRET, REQUEST_SECRET, ACCESS_TOKEN, ACCESS_SECRET)


@dataclass
class FakePlatform:
    """An ``AccountAuthorization`` that records what it was asked and answers ``account``."""

    account: AccountDescription = ACME
    state: str = STATE
    starts: list[tuple[PlatformApp, str]] = field(default_factory=list)
    finishes: list[tuple[PlatformApp, str, str, str]] = field(default_factory=list)

    def start(self, app: PlatformApp, *, callback_url: str) -> AuthorizationStart:
        self.starts.append((app, callback_url))
        return AuthorizationStart(
            authorize_url=f"https://x.example/authenticate?oauth_token={self.state}",
            state=self.state,
            secret_to_keep=REQUEST_SECRET,
        )

    def finish(
        self, app: PlatformApp, *, state: str, grant: str, kept_secret: str
    ) -> AuthorizedAccount:
        self.finishes.append((app, state, grant, kept_secret))
        return AuthorizedAccount(
            account=self.account, access_token=ACCESS_TOKEN, access_secret=ACCESS_SECRET
        )


def new_workspace(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


def new_project(session: Session, workspace_id: uuid.UUID, name: str = "app") -> uuid.UUID:
    url = f"https://github.com/acme/{name}"
    return create_project(session, workspace_id=workspace_id, name=name, source_repo_url=url).id


def new_cipher() -> CredentialCipher:
    return CredentialCipher(base64.b64encode(os.urandom(32)).decode())


@pytest.fixture
def workspace_id(session) -> uuid.UUID:
    return new_workspace(session)


@pytest.fixture
def project_id(session, workspace_id) -> uuid.UUID:
    return new_project(session, workspace_id)


@pytest.fixture
def cipher() -> CredentialCipher:
    return new_cipher()


@pytest.fixture
def platform() -> FakePlatform:
    return FakePlatform()


def start(session, workspace_id, project_id, platform, cipher, environ=ENVIRON, callback=CALLBACK):
    return start_connection(
        session,
        environ,
        workspace_id=workspace_id,
        project_id=project_id,
        platform=SocialPlatform.X,
        callback_url=callback,
        authorization=platform,
        cipher=cipher,
    )


def finish(session, workspace_id, platform, cipher, state=STATE, environ=ENVIRON):
    return finish_connection(
        session,
        environ,
        workspace_id=workspace_id,
        state=state,
        grant=VERIFIER,
        connected_by=OWNER,
        authorization=platform,
        cipher=cipher,
    )


def connect(session, workspace_id, project_id, platform, cipher):
    start(session, workspace_id, project_id, platform, cipher)
    return finish(session, workspace_id, platform, cipher, state=platform.state)


def tokens_of(session, workspace_id, project_id, cipher):
    return account_tokens(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        platform=SocialPlatform.X,
        cipher=cipher,
    )


def stored_bytes(session) -> bytes:
    """Every byte the two tables hold for secrets."""
    rows = session.execute(
        select(
            SocialAccountRecord.access_token_ciphertext,
            SocialAccountRecord.access_secret_ciphertext,
        )
    ).all()
    attempts = session.scalars(select(SocialConnectionAttemptRecord.secret_ciphertext)).all()
    return b"".join([*(value for row in rows for value in row), *attempts])


# ------------------------------------------------------------------ start
def test_start_answers_the_platform_s_page_and_asks_with_the_app_and_callback(
    session, workspace_id, project_id, platform, cipher
):
    url = start(session, workspace_id, project_id, platform, cipher)
    assert url == f"https://x.example/authenticate?oauth_token={STATE}"
    ((app, callback),) = platform.starts
    assert (app.key, app.secret, callback) == (APP_KEY, APP_SECRET, CALLBACK)


def test_start_keeps_the_attempt_with_its_secret_sealed(
    session, workspace_id, project_id, platform, cipher
):
    start(session, workspace_id, project_id, platform, cipher)
    attempt = session.scalars(select(SocialConnectionAttemptRecord)).one()
    assert (attempt.state, attempt.project_id, attempt.platform) == (STATE, project_id, "x")
    assert REQUEST_SECRET.encode() not in stored_bytes(session)


def test_app_stored_from_the_dashboard_is_used_without_the_environment(
    session, workspace_id, project_id, platform, cipher
):
    for name, value in (("x_api_key", APP_KEY), ("x_api_secret", APP_SECRET)):
        set_credential(
            session,
            {},
            workspace_id=workspace_id,
            name=name,
            value=value,
            set_by=OWNER,
            cipher=cipher,
        )
    start(session, workspace_id, project_id, platform, cipher, environ={})
    ((app, _),) = platform.starts
    assert (app.key, app.secret) == (APP_KEY, APP_SECRET)


@pytest.mark.parametrize(
    ("environ", "missing"),
    [({}, "x_api_key"), ({"X_API_KEY": APP_KEY}, "x_api_secret")],
)
def test_start_without_the_app_says_which_credential_to_set_and_asks_nothing(
    session, workspace_id, project_id, platform, cipher, environ, missing
):
    with pytest.raises(PlatformAppNotSet, match=f"set {missing} in the dashboard's Settings"):
        start(session, workspace_id, project_id, platform, cipher, environ=environ)
    assert platform.starts == []


def test_start_without_an_encryption_key_is_refused(session, workspace_id, project_id, platform):
    with pytest.raises(EncryptionKeyMissing):
        start(session, workspace_id, project_id, platform, None)
    assert platform.starts == []


def test_start_for_a_project_of_another_workspace_is_not_found(
    session, workspace_id, project_id, platform, cipher
):
    other = new_workspace(session)
    with pytest.raises(ProjectNotFound):
        start(session, other, project_id, platform, cipher)
    assert platform.starts == []


@pytest.mark.parametrize("callback", ["", "/callback", "javascript:alert(1)", "ftp://host/cb"])
def test_callback_that_is_not_an_absolute_http_url_is_refused(
    session, workspace_id, project_id, platform, cipher, callback
):
    with pytest.raises(CallbackUrlRefused):
        start(session, workspace_id, project_id, platform, cipher, callback=callback)
    assert platform.starts == []


# ----------------------------------------------------------------- finish
def test_finish_connects_the_account_to_the_project_it_was_started_for(
    session, workspace_id, project_id, platform, cipher
):
    connected = connect(session, workspace_id, project_id, platform, cipher)
    assert (connected.project_id, connected.platform) == (project_id, SocialPlatform.X)
    assert (connected.account_id, connected.username) == ("42", "acme")
    assert connected.connected_by == OWNER
    assert list_accounts(session, workspace_id=workspace_id, project_id=project_id) == [connected]


def test_finish_hands_the_platform_the_grant_and_the_secret_kept_at_start(
    session, workspace_id, project_id, platform, cipher
):
    connect(session, workspace_id, project_id, platform, cipher)
    ((app, state, grant, kept_secret),) = platform.finishes
    assert (app.key, state, grant, kept_secret) == (APP_KEY, STATE, VERIFIER, REQUEST_SECRET)


def test_finish_stores_the_tokens_sealed_and_only_account_tokens_opens_them(
    session, workspace_id, project_id, platform, cipher
):
    connect(session, workspace_id, project_id, platform, cipher)
    held = stored_bytes(session)
    assert ACCESS_TOKEN.encode() not in held
    assert ACCESS_SECRET.encode() not in held
    tokens = tokens_of(session, workspace_id, project_id, cipher)
    assert tokens is not None
    assert (tokens.access_token, tokens.access_secret) == (ACCESS_TOKEN, ACCESS_SECRET)


def test_attempt_is_used_once(session, workspace_id, project_id, platform, cipher):
    connect(session, workspace_id, project_id, platform, cipher)
    with pytest.raises(ConnectionAttemptNotFound):
        finish(session, workspace_id, platform, cipher)
    assert len(platform.finishes) == 1


def test_unknown_state_is_not_found_and_not_repeated(session, workspace_id, platform, cipher):
    with pytest.raises(ConnectionAttemptNotFound) as refused:
        finish(session, workspace_id, platform, cipher, state="made-up-<script>")
    assert "made-up" not in str(refused.value)
    assert platform.finishes == []


def test_attempt_of_another_workspace_is_not_found(
    session, workspace_id, project_id, platform, cipher
):
    start(session, workspace_id, project_id, platform, cipher)
    with pytest.raises(ConnectionAttemptNotFound):
        finish(session, new_workspace(session), platform, cipher)
    assert platform.finishes == []


def test_expired_attempt_is_refused_before_the_platform_is_asked(
    session, workspace_id, project_id, platform, cipher
):
    start(session, workspace_id, project_id, platform, cipher)
    session.execute(text("UPDATE social_connection_attempt SET expires_at = now()"))
    with pytest.raises(ConnectionAttemptExpired, match="more than 15 minutes ago"):
        finish(session, workspace_id, platform, cipher)
    assert platform.finishes == []


def test_attempt_sealed_under_another_key_does_not_open(
    session, workspace_id, project_id, platform, cipher
):
    start(session, workspace_id, project_id, platform, cipher)
    with pytest.raises(ConnectionSecretUnreadable):
        finish(session, workspace_id, platform, new_cipher())
    assert platform.finishes == []


def test_finish_without_an_encryption_key_is_refused(
    session, workspace_id, project_id, platform, cipher
):
    start(session, workspace_id, project_id, platform, cipher)
    with pytest.raises(EncryptionKeyMissing):
        finish(session, workspace_id, platform, None)


def test_abandoned_attempt_cannot_be_finished(session, workspace_id, project_id, platform, cipher):
    start(session, workspace_id, project_id, platform, cipher)
    assert abandon_connection(session, workspace_id=workspace_id, state=STATE) is True
    assert abandon_connection(session, workspace_id=workspace_id, state=STATE) is False
    with pytest.raises(ConnectionAttemptNotFound):
        finish(session, workspace_id, platform, cipher)


# ------------------------------------------------------------- reconnect
def test_connecting_the_same_account_again_updates_it(
    session, workspace_id, project_id, platform, cipher
):
    first = connect(session, workspace_id, project_id, platform, cipher)
    platform.state = "second-request-token"
    platform.account = AccountDescription("42", "acme_hq", "Acme HQ", None)
    again = connect(session, workspace_id, project_id, platform, cipher)
    assert again.id == first.id
    assert again.username == "acme_hq"
    assert list_accounts(session, workspace_id=workspace_id, project_id=project_id) == [again]


def test_connecting_another_account_replaces_the_project_s_account(
    session, workspace_id, project_id, platform, cipher
):
    connect(session, workspace_id, project_id, platform, cipher)
    platform.state = "second-request-token"
    platform.account = AccountDescription("7", "other", "Other", None)
    other = connect(session, workspace_id, project_id, platform, cipher)
    assert list_accounts(session, workspace_id=workspace_id, project_id=project_id) == [other]


# ------------------------------------------------------- list, disconnect
def test_project_without_accounts_lists_none(session, workspace_id, project_id):
    assert list_accounts(session, workspace_id=workspace_id, project_id=project_id) == []


def test_listing_or_disconnecting_a_project_of_another_workspace_is_not_found(
    session, workspace_id, project_id, platform, cipher
):
    connect(session, workspace_id, project_id, platform, cipher)
    other = new_workspace(session)
    with pytest.raises(ProjectNotFound):
        list_accounts(session, workspace_id=other, project_id=project_id)
    with pytest.raises(ProjectNotFound):
        disconnect(session, workspace_id=other, project_id=project_id, platform=SocialPlatform.X)
    assert tokens_of(session, other, project_id, cipher) is None
    assert len(list_accounts(session, workspace_id=workspace_id, project_id=project_id)) == 1


def test_disconnecting_forgets_the_account_and_its_tokens(
    session, workspace_id, project_id, platform, cipher
):
    connect(session, workspace_id, project_id, platform, cipher)
    ids = {"workspace_id": workspace_id, "project_id": project_id}
    assert disconnect(session, **ids, platform=SocialPlatform.X) is True
    assert list_accounts(session, **ids) == []
    assert tokens_of(session, workspace_id, project_id, cipher) is None
    assert disconnect(session, **ids, platform=SocialPlatform.X) is False


# ---------------------------------------------------------------- secrets
def test_tokens_copied_to_another_project_do_not_open(
    session, workspace_id, project_id, platform, cipher
):
    connect(session, workspace_id, project_id, platform, cipher)
    second = new_project(session, workspace_id, "second")
    session.execute(
        text(
            "INSERT INTO social_account (workspace_id, project_id, platform, account_id,"
            " username, display_name, access_token_nonce, access_token_ciphertext,"
            " access_secret_nonce, access_secret_ciphertext, connected_by)"
            " SELECT workspace_id, :second, platform, account_id, username, display_name,"
            " access_token_nonce, access_token_ciphertext, access_secret_nonce,"
            " access_secret_ciphertext, connected_by FROM social_account"
        ),
        {"second": second},
    )
    with pytest.raises(ConnectionSecretUnreadable):
        tokens_of(session, workspace_id, second, cipher)


def test_tokens_need_the_encryption_key(session, workspace_id, project_id, platform, cipher):
    connect(session, workspace_id, project_id, platform, cipher)
    with pytest.raises(EncryptionKeyMissing):
        tokens_of(session, workspace_id, project_id, None)


def test_no_secret_is_in_what_a_caller_is_shown(
    session, workspace_id, project_id, platform, cipher
):
    connected = connect(session, workspace_id, project_id, platform, cipher)
    tokens = tokens_of(session, workspace_id, project_id, cipher)
    started = platform.start(PlatformApp(key=APP_KEY, secret=APP_SECRET), callback_url=CALLBACK)
    authorized = platform.finish(
        PlatformApp(key=APP_KEY, secret=APP_SECRET), state=STATE, grant=VERIFIER, kept_secret=""
    )
    shown = " ".join(
        repr(value) for value in (connected, tokens, started, authorized, platform.starts)
    )
    assert not [secret for secret in SECRETS if secret in shown]


def test_failures_do_not_carry_a_secret(session, workspace_id, project_id, platform, cipher):
    start(session, workspace_id, project_id, platform, cipher)
    failures = []
    for attempt in (
        lambda: finish(session, workspace_id, platform, new_cipher()),
        lambda: start(session, workspace_id, project_id, platform, cipher, environ={}),
    ):
        with pytest.raises(Exception) as failed:
            attempt()
        failures.append(f"{failed.value!s} {failed.value!r} {failed.value.__cause__!r}")
    assert not [secret for secret in SECRETS if secret in " ".join(failures)]
