"""Tests for the social account routes. They need PostgreSQL (see the root ``conftest.py``).

Each test works in a workspace of its own, in the test's session (rolled back afterwards),
with the X app's credentials in the environment, a key made for the test and a fake X that
reaches no network. The tokens below are made up and easy to find in a response.
"""

import base64
import os
import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from openmarketer_api.dependencies import (
    account_authorizations,
    credential_cipher,
    dashboard_url,
    db_session,
    environment,
)
from openmarketer_api.identity import LOCAL_USER_ID, current_workspace_id
from openmarketer_core.credentials.cipher import CredentialCipher
from openmarketer_core.db.models import Project, Workspace
from openmarketer_core.social_accounts.catalog import AccountDescription, SocialPlatform
from openmarketer_core.social_accounts.connection import (
    AuthorizationRefused,
    AuthorizationStart,
    AuthorizationUnavailable,
    AuthorizedAccount,
    PlatformApp,
)

DASHBOARD = "http://127.0.0.1:3000"
CALLBACK = "/v1/social-accounts/x/callback"
REQUEST_TOKEN = "RequestTokenMarker-0123456789"
REQUEST_SECRET = "RequestSecretMarker-0123456789"
VERIFIER = "VerifierMarker-0123456789"
ACCESS_TOKEN = "AccessTokenMarker-0123456789"
ACCESS_SECRET = "AccessSecretMarker-0123456789"
APP_KEY = "AppKeyMarker-0123456789"
APP_SECRET = "AppSecretMarker-0123456789"
SECRETS = (
    REQUEST_TOKEN,
    REQUEST_SECRET,
    VERIFIER,
    ACCESS_TOKEN,
    ACCESS_SECRET,
    APP_KEY,
    APP_SECRET,
)
ACME = AccountDescription(account_id="1234", username="acme", display_name="Acme", picture_url=None)


@dataclass
class FakeX:
    """An ``AccountAuthorization`` that records what it was asked; ``failure`` is raised instead."""

    failure: Exception | None = None
    starts: list[str] = field(default_factory=list)
    finishes: list[tuple[str, str, str]] = field(default_factory=list)

    def start(self, app: PlatformApp, *, callback_url: str) -> AuthorizationStart:
        if self.failure is not None:
            raise self.failure
        self.starts.append(callback_url)
        return AuthorizationStart(
            authorize_url=f"https://x.example/authenticate?oauth_token={REQUEST_TOKEN}",
            state=REQUEST_TOKEN,
            secret_to_keep=REQUEST_SECRET,
        )

    def finish(
        self, app: PlatformApp, *, state: str, grant: str, kept_secret: str
    ) -> AuthorizedAccount:
        if self.failure is not None:
            raise self.failure
        self.finishes.append((state, grant, kept_secret))
        return AuthorizedAccount(
            account=ACME, access_token=ACCESS_TOKEN, access_secret=ACCESS_SECRET
        )


def new_project(session: Session, workspace_id: uuid.UUID) -> uuid.UUID:
    project = Project(
        workspace_id=workspace_id, name="App", source_repo_url="https://example.com/a.git"
    )
    session.add(project)
    session.flush()
    return project.id


def new_workspace(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


@pytest.fixture
def workspace(app, session) -> uuid.UUID:
    workspace_id = new_workspace(session)
    app.dependency_overrides[db_session] = lambda: session
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[dashboard_url] = lambda: DASHBOARD
    return workspace_id


@pytest.fixture
def project_id(session, workspace) -> uuid.UUID:
    return new_project(session, workspace)


@pytest.fixture(autouse=True)
def environ(app) -> dict[str, str]:
    variables = {"X_API_KEY": APP_KEY, "X_API_SECRET": APP_SECRET}
    app.dependency_overrides[environment] = lambda: variables
    return variables


@pytest.fixture(autouse=True)
def cipher(app) -> None:
    with_new_key = CredentialCipher(base64.b64encode(os.urandom(32)).decode())
    app.dependency_overrides[credential_cipher] = lambda: with_new_key


@pytest.fixture(autouse=True)
def fake_x(app) -> FakeX:
    fake = FakeX()
    app.dependency_overrides[account_authorizations] = lambda: {SocialPlatform.X: fake}
    return fake


def accounts_url(project_id: uuid.UUID, then: str = "") -> str:
    return f"/v1/projects/{project_id}/social-accounts{then}"


def connect(client, project_id: uuid.UUID):
    return client.post(accounts_url(project_id, "/x/connect"))


def finish(client):
    return client.post(CALLBACK, json={"state": REQUEST_TOKEN, "grant": VERIFIER})


def assert_no_secret(response) -> None:
    for secret in SECRETS:
        assert secret not in response.text


# ------------------------------------------------------------------- connect
def test_connect_answers_the_platform_page_and_asks_back_to_the_dashboard(
    client, project_id, fake_x
):
    response = connect(client, project_id)
    assert response.status_code == 200
    assert response.json() == {
        "authorize_url": f"https://x.example/authenticate?oauth_token={REQUEST_TOKEN}"
    }
    assert fake_x.starts == [f"{DASHBOARD}/connections/x/callback"]


def test_connect_takes_no_body(client, project_id, fake_x):
    response = client.post(
        accounts_url(project_id, "/x/connect"), json={"callback_url": "https://evil.example"}
    )
    assert response.status_code == 422
    assert fake_x.starts == []


def test_connecting_a_project_of_another_workspace_is_not_found(client, session, workspace):
    other = new_project(session, new_workspace(session))
    response = connect(client, other)
    assert (response.status_code, response.json()["code"]) == (404, "project_not_found")


def test_connecting_an_unknown_platform_is_refused(client, project_id):
    response = client.post(accounts_url(project_id, "/myspace/connect"))
    assert response.status_code == 422


def test_connecting_without_the_app_credentials_names_the_one_to_set(
    client, project_id, environ, fake_x
):
    del environ["X_API_SECRET"]
    response = connect(client, project_id)
    assert (response.status_code, response.json()["code"]) == (409, "platform_app_not_set")
    assert "x_api_secret" in response.json()["message"]
    assert fake_x.starts == []


def test_connecting_without_an_encryption_key_is_a_conflict(app, client, project_id):
    app.dependency_overrides[credential_cipher] = lambda: None
    response = connect(client, project_id)
    assert (response.status_code, response.json()["code"]) == (409, "encryption_key_missing")


def test_platform_that_refuses_the_start_is_a_bad_request(client, project_id, fake_x):
    fake_x.failure = AuthorizationRefused("X refused the request token: HTTP 401")
    response = connect(client, project_id)
    assert response.status_code == 400
    assert response.json() == {
        "code": "authorization_refused",
        "message": "X refused the request token: HTTP 401",
    }


def test_platform_that_cannot_be_reached_is_a_bad_gateway(client, project_id, fake_x):
    fake_x.failure = AuthorizationUnavailable("X could not be reached: ConnectError")
    response = connect(client, project_id)
    assert (response.status_code, response.json()["code"]) == (502, "platform_unavailable")


# ------------------------------------------------------------------- callback
def test_connect_then_callback_connects_the_account_to_the_project(client, project_id, fake_x):
    connect(client, project_id)
    response = finish(client)
    assert response.status_code == 200
    body = response.json()
    assert (body["outcome"], body["project_id"]) == ("connected", str(project_id))
    account = body["account"]
    assert {key: account[key] for key in ("project_id", "platform", "username")} == {
        "project_id": str(project_id),
        "platform": "x",
        "username": "acme",
    }
    assert account["connected_by"] == str(LOCAL_USER_ID)
    assert fake_x.finishes == [(REQUEST_TOKEN, VERIFIER, REQUEST_SECRET)]


def test_connected_account_is_listed_and_no_response_holds_a_token(client, project_id):
    connect(client, project_id)
    finished = finish(client)
    listed = client.get(accounts_url(project_id))
    assert [account["account_id"] for account in listed.json()["accounts"]] == ["1234"]
    for response in (finished, listed):
        assert_no_secret(response)


def test_connect_answers_the_request_token_in_its_url_and_no_other_secret(client, project_id):
    # The request token is in the URL of X's page by design; its secret never leaves the API.
    response = connect(client, project_id)
    for secret in (REQUEST_SECRET, APP_KEY, APP_SECRET):
        assert secret not in response.text


def test_a_connection_is_finished_once(client, project_id):
    connect(client, project_id)
    finish(client)
    response = finish(client)
    assert (response.status_code, response.json()["code"]) == (404, "connection_not_found")


def test_callback_for_a_connection_never_started_is_not_found(client, workspace):
    response = finish(client)
    assert (response.status_code, response.json()["code"]) == (404, "connection_not_found")


def test_callback_for_a_connection_of_another_workspace_is_not_found(
    app, client, session, project_id
):
    connect(client, project_id)
    other = new_workspace(session)
    app.dependency_overrides[current_workspace_id] = lambda: other
    response = finish(client)
    assert (response.status_code, response.json()["code"]) == (404, "connection_not_found")


def test_callback_for_an_expired_connection_is_gone(client, session, project_id, fake_x):
    connect(client, project_id)
    session.execute(text("UPDATE social_connection_attempt SET expires_at = now()"))
    response = finish(client)
    assert (response.status_code, response.json()["code"]) == (410, "connection_expired")
    assert fake_x.finishes == []


def test_declined_connection_is_forgotten(client, project_id):
    connect(client, project_id)
    declined = client.post(CALLBACK, json={"state": REQUEST_TOKEN})
    assert declined.status_code == 200
    assert declined.json() == {
        "outcome": "declined",
        "project_id": str(project_id),
        "account": None,
    }
    after = finish(client)
    assert (after.status_code, after.json()["code"]) == (404, "connection_not_found")


def test_platform_that_refuses_the_grant_is_a_bad_request(client, project_id, fake_x):
    connect(client, project_id)
    fake_x.failure = AuthorizationRefused("X refused the access token: HTTP 401")
    response = finish(client)
    assert (response.status_code, response.json()["code"]) == (400, "authorization_refused")
    assert_no_secret(response)


def test_platform_unreachable_on_the_callback_is_a_bad_gateway(client, project_id, fake_x):
    connect(client, project_id)
    fake_x.failure = AuthorizationUnavailable("X answered the access token with HTTP 503")
    response = finish(client)
    assert (response.status_code, response.json()["code"]) == (502, "platform_unavailable")


def test_refused_callback_body_is_not_quoted_back(client, workspace):
    response = client.post(
        CALLBACK, json={"state": REQUEST_TOKEN, "grant": VERIFIER, ACCESS_TOKEN: ACCESS_SECRET}
    )
    assert response.status_code == 422
    assert_no_secret(response)


def test_callback_with_a_state_that_is_not_text_is_not_quoted_back(client, workspace):
    response = client.post(CALLBACK, json={"state": {"token": REQUEST_TOKEN}, "grant": VERIFIER})
    assert response.status_code == 422
    assert_no_secret(response)


# ------------------------------------------------------------------- list
def test_list_says_which_platforms_are_ready_to_connect(client, project_id):
    response = client.get(accounts_url(project_id))
    assert response.status_code == 200
    assert response.json() == {
        "accounts": [],
        "platforms": [{"platform": "x", "name": "X", "ready": True, "missing_credentials": []}],
        "can_connect": True,
    }


def test_list_names_the_app_credentials_still_to_set(client, project_id, environ):
    environ.clear()
    [platform] = client.get(accounts_url(project_id)).json()["platforms"]
    assert platform["ready"] is False
    assert platform["missing_credentials"] == [
        {"name": "x_api_key", "environment_variable": "X_API_KEY"},
        {"name": "x_api_secret", "environment_variable": "X_API_SECRET"},
    ]


def test_list_says_connecting_needs_an_encryption_key(app, client, project_id):
    app.dependency_overrides[credential_cipher] = lambda: None
    assert client.get(accounts_url(project_id)).json()["can_connect"] is False


def test_listing_a_project_of_another_workspace_is_not_found(client, session, workspace):
    other = new_project(session, new_workspace(session))
    response = client.get(accounts_url(other))
    assert (response.status_code, response.json()["code"]) == (404, "project_not_found")


# ------------------------------------------------------------------- disconnect
def test_disconnect_forgets_the_account(client, project_id):
    connect(client, project_id)
    finish(client)
    response = client.delete(accounts_url(project_id, "/x"))
    assert (response.status_code, response.content) == (204, b"")
    assert client.get(accounts_url(project_id)).json()["accounts"] == []


def test_disconnecting_a_platform_with_no_account_is_not_found(client, project_id):
    response = client.delete(accounts_url(project_id, "/x"))
    assert (response.status_code, response.json()["code"]) == (404, "social_account_not_found")


def test_disconnecting_in_a_project_of_another_workspace_is_not_found(client, session, workspace):
    other = new_project(session, new_workspace(session))
    response = client.delete(accounts_url(other, "/x"))
    assert (response.status_code, response.json()["code"]) == (404, "project_not_found")


def test_decline_of_a_connection_never_started_is_not_found(client, workspace):
    response = client.post(CALLBACK, json={"state": REQUEST_TOKEN})
    assert (response.status_code, response.json()["code"]) == (404, "connection_not_found")


def test_answer_from_another_platform_than_the_attempt_s_is_not_found(
    client, session, project_id, fake_x
):
    connect(client, project_id)
    session.execute(
        text(
            "ALTER TABLE social_connection_attempt"
            " DROP CONSTRAINT ck_social_connection_attempt_platform"
        )
    )
    session.execute(text("UPDATE social_connection_attempt SET platform = 'linkedin'"))
    finished = finish(client)
    declined = client.post(CALLBACK, json={"state": REQUEST_TOKEN})
    for response in (finished, declined):
        assert (response.status_code, response.json()["code"]) == (404, "connection_not_found")
    assert fake_x.finishes == []
