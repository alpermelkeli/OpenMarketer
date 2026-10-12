"""Tests for the credential routes. They need PostgreSQL (see the root ``conftest.py``).

Each test starts with an empty environment, no stored value and a key made for
the test. ``SECRET`` is a made-up value that is easy to find in a response.
"""

import asyncio
import base64
import logging
import os
import uuid

import pytest
from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from openmarketer_api.dependencies import credential_cipher, db_session, environment
from openmarketer_api.identity import LOCAL_USER_ID
from openmarketer_api.routes.credentials import SetCredentialRequest
from openmarketer_api.secret_bodies import SecretBodyRoute
from openmarketer_core.credentials.catalog import SealedValue
from openmarketer_core.credentials.cipher import CredentialCipher
from openmarketer_core.db.credentials import sealed_credential, store_credential
from openmarketer_core.db.evidence_store import local_workspace_id
from openmarketer_core.db.models import Workspace

URL = "/v1/settings/credentials"
GITHUB = f"{URL}/github_token"
SECRET = "sk-or-v1-Recognisable-0123456789-abcdef"
OTHER_SECRET = "ghp_AnotherRecognisable9876543210"
# A secret that has the form of a credential's name, as one pasted into the path may have.
SECRET_SHAPED_NAME = "secretmarker0123456789"
NOT_SET = [
    {
        "name": "openrouter_api_key",
        "source": "not_set",
        "environment_variable": "OPENROUTER_API_KEY",
        "stored": None,
    },
    {
        "name": "github_token",
        "source": "not_set",
        "environment_variable": "GITHUB_TOKEN",
        "stored": None,
    },
    {
        "name": "gitlab_token",
        "source": "not_set",
        "environment_variable": "GITLAB_TOKEN",
        "stored": None,
    },
]


@pytest.fixture(autouse=True)
def database(app, session) -> None:
    """Requests work in the test's session, which is rolled back afterwards."""
    app.dependency_overrides[db_session] = lambda: session


@pytest.fixture(autouse=True)
def environ(app) -> dict[str, str]:
    """The environment the API sees; a test adds the variables it needs."""
    variables: dict[str, str] = {}
    app.dependency_overrides[environment] = lambda: variables
    return variables


@pytest.fixture(autouse=True)
def cipher(app) -> CredentialCipher:
    """The API has a key, generated here."""
    with_new_key = CredentialCipher(base64.b64encode(os.urandom(32)).decode())
    app.dependency_overrides[credential_cipher] = lambda: with_new_key
    return with_new_key


@pytest.fixture
def without_encryption_key(app) -> None:
    app.dependency_overrides[credential_cipher] = lambda: None


def sealed(session: Session, name: str = "github_token") -> SealedValue | None:
    """What the local workspace has stored under ``name``, still encrypted."""
    return sealed_credential(session, workspace_id=local_workspace_id(session), name=name)


def status_of(client: TestClient, name: str) -> dict:
    listed = client.get(URL).json()["credentials"]
    return next(status for status in listed if status["name"] == name)


# ------------------------------------------------------------------- list
def test_list_names_every_credential_in_a_fixed_order_when_none_is_set(client):
    response = client.get(URL)
    assert (response.status_code, response.json()["credentials"]) == (200, NOT_SET)


def test_credential_the_environment_sets_is_listed_as_such(client, environ):
    environ["GITHUB_TOKEN"] = SECRET
    assert status_of(client, "github_token") == {
        "name": "github_token",
        "source": "environment",
        "environment_variable": "GITHUB_TOKEN",
        "stored": None,
    }


def test_empty_environment_variable_sets_nothing(client, environ):
    environ["GITHUB_TOKEN"] = ""
    assert status_of(client, "github_token")["source"] == "not_set"


def test_stored_credential_is_listed_with_who_stored_it_and_when(client):
    stored = client.put(GITHUB, json={"value": SECRET}).json()["stored"]
    status = status_of(client, "github_token")
    assert (status["source"], status["stored"]) == ("stored", stored)
    assert set(stored) == {"set_at", "set_by"}


def test_stored_value_the_environment_overrides_is_still_shown_as_stored(client, environ):
    stored = client.put(GITHUB, json={"value": SECRET}).json()["stored"]
    environ["GITHUB_TOKEN"] = OTHER_SECRET
    status = status_of(client, "github_token")
    assert (status["source"], status["stored"]) == ("environment", stored)


def test_storing_one_credential_leaves_the_others_not_set(client):
    client.put(GITHUB, json={"value": SECRET})
    sources = {status["name"]: status["source"] for status in client.get(URL).json()["credentials"]}
    assert sources == {
        "openrouter_api_key": "not_set",
        "github_token": "stored",
        "gitlab_token": "not_set",
    }


def test_credential_stored_in_another_workspace_is_not_listed(client, session, cipher):
    elsewhere = Workspace(name="Elsewhere")
    session.add(elsewhere)
    session.flush()
    store_credential(
        session,
        workspace_id=elsewhere.id,
        name="github_token",
        sealed=cipher.seal(SECRET, workspace_id=elsewhere.id, credential_name="github_token"),
        set_by=LOCAL_USER_ID,
    )
    assert client.get(URL).json()["credentials"] == NOT_SET


def test_list_says_that_credentials_can_be_stored_when_the_api_has_a_key(client):
    assert client.get(URL).json()["can_store"] is True


def test_list_says_that_credentials_cannot_be_stored_without_a_key(client, without_encryption_key):
    assert client.get(URL).json()["can_store"] is False


def test_list_works_without_a_key_when_a_value_is_stored(client, app):
    client.put(GITHUB, json={"value": SECRET})
    app.dependency_overrides[credential_cipher] = lambda: None
    assert status_of(client, "github_token")["source"] == "stored"


# -------------------------------------------------------------------- set
def test_set_answers_with_the_new_status(client):
    response = client.put(GITHUB, json={"value": SECRET})
    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"name", "source", "environment_variable", "stored"}
    assert (body["name"], body["source"], body["environment_variable"]) == (
        "github_token",
        "stored",
        "GITHUB_TOKEN",
    )


def test_set_stores_the_value_encrypted(client, session, cipher):
    client.put(GITHUB, json={"value": SECRET})
    stored = sealed(session)
    assert stored is not None
    assert SECRET.encode() not in stored.ciphertext
    opened = cipher.open(
        stored, workspace_id=local_workspace_id(session), credential_name="github_token"
    )
    assert opened == SECRET


def test_set_drops_whitespace_around_the_value(client, session, cipher):
    client.put(GITHUB, json={"value": f"  {SECRET}\n"})
    stored = sealed(session)
    assert stored is not None
    opened = cipher.open(
        stored, workspace_id=local_workspace_id(session), credential_name="github_token"
    )
    assert opened == SECRET


def test_set_replaces_the_value_stored_before(client, session, cipher):
    client.put(GITHUB, json={"value": SECRET})
    assert client.put(GITHUB, json={"value": OTHER_SECRET}).status_code == 200
    stored = sealed(session)
    assert stored is not None
    opened = cipher.open(
        stored, workspace_id=local_workspace_id(session), credential_name="github_token"
    )
    assert opened == OTHER_SECRET


def test_set_is_recorded_under_the_local_user(client):
    stored = client.put(GITHUB, json={"value": SECRET}).json()["stored"]
    assert stored["set_by"] == str(LOCAL_USER_ID)


def test_request_cannot_name_who_sets_the_credential(client, session):
    response = client.put(GITHUB, json={"value": SECRET, "set_by": str(uuid.uuid4())})
    assert response.status_code == 422
    assert sealed(session) is None


def test_set_is_refused_while_the_environment_sets_the_credential(client, session, environ):
    environ["GITHUB_TOKEN"] = OTHER_SECRET
    response = client.put(GITHUB, json={"value": SECRET})
    assert (response.status_code, response.json()) == (
        409,
        {
            "code": "credential_set_by_environment",
            "message": (
                "github_token is set by the environment variable GITHUB_TOKEN;"
                " unset it to store a value instead"
            ),
        },
    )
    assert sealed(session) is None


def test_set_is_refused_without_an_encryption_key(client, session, without_encryption_key):
    response = client.put(GITHUB, json={"value": SECRET})
    assert (response.status_code, response.json()["code"]) == (409, "encryption_key_missing")
    assert "ENCRYPTION_KEY" in response.json()["message"]
    assert sealed(session) is None


def test_set_of_an_unknown_credential_is_not_found(client):
    response = client.put(f"{URL}/slack_token", json={"value": SECRET})
    assert (response.status_code, response.json()) == (
        404,
        {"code": "credential_not_found", "message": "there is no such credential"},
    )


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        ("", "a credential cannot be empty"),
        ("   ", "a credential cannot be empty"),
        (f"{SECRET} {SECRET}", "a credential holds only printable ASCII characters"),
        (f"{SECRET}\n{SECRET}", "a credential holds only printable ASCII characters"),
        (f"{SECRET}é", "a credential holds only printable ASCII characters"),
        (SECRET * 200, "a credential is at most 4096 characters long"),
    ],
)
def test_value_a_credential_cannot_have_is_refused_by_its_rule(client, session, value, rule):
    response = client.put(GITHUB, json={"value": value})
    (refusal,) = response.json()["detail"]
    assert response.status_code == 422
    assert (refusal["type"], refusal["loc"]) == ("value_error", ["body", "value"])
    assert refusal["msg"].startswith(rule)
    assert set(refusal) == {"type", "loc", "msg"}
    assert SECRET not in response.text
    assert sealed(session) is None


# ------------------------------------------- the secret is never answered back
MALFORMED = {
    "value of another type": {"json": {"value": [SECRET]}},
    "value in an object": {"json": {"value": {"secret": SECRET}}},
    "unknown field": {"json": {"value": SECRET, "note": SECRET}},
    "secret as a field name": {"json": {SECRET: "value"}},
    "misspelt field": {"json": {"valeu": SECRET}},
    "body that is a string": {"json": SECRET},
    "body that is a list": {"json": [{"value": SECRET}]},
    "JSON cut short": {
        "content": f'{{"value": "{SECRET}',
        "headers": {"Content-Type": "application/json"},
    },
    "text that is not JSON": {
        "content": SECRET,
        "headers": {"Content-Type": "application/json"},
    },
    "form": {"data": {"value": SECRET}},
    "plain text": {"content": SECRET, "headers": {"Content-Type": "text/plain"}},
}


@pytest.mark.parametrize("request_body", MALFORMED.values(), ids=MALFORMED.keys())
def test_malformed_body_is_refused_without_repeating_any_of_it(client, session, request_body):
    response = client.put(GITHUB, **request_body)
    assert response.status_code == 422
    assert SECRET not in response.text
    assert all(set(error) == {"type", "loc", "msg"} for error in response.json()["detail"])
    assert sealed(session) is None


def test_refusal_of_a_field_names_only_fields_the_route_has(client):
    response = client.put(GITHUB, json={"value": 7, SECRET: 1})
    assert sorted(error["loc"] for error in response.json()["detail"]) == [
        ["body"],
        ["body", "value"],
    ]


def test_secret_sent_as_the_name_is_refused_without_repeating_it(client):
    response = client.put(f"{URL}/{SECRET}", json={"value": "anything"})
    assert response.status_code == 422
    assert SECRET not in response.text


@pytest.mark.parametrize(
    ("method", "body"), [("PUT", {"value": "anything"}), ("DELETE", None)], ids=["set", "remove"]
)
def test_secret_shaped_like_a_name_is_not_found_without_repeating_it(client, method, body):
    response = client.request(method, f"{URL}/{SECRET_SHAPED_NAME}", json=body)
    assert (response.status_code, response.json()["code"]) == (404, "credential_not_found")
    assert SECRET_SHAPED_NAME not in response.text


def test_refusal_of_a_malformed_body_does_not_lead_back_to_the_body(monkeypatch):
    async def refuse_quoting_the_body(request: Request) -> Response:
        raise RequestValidationError(
            [
                {
                    "type": "missing",
                    "loc": ("body", "value"),
                    "msg": "Field required",
                    "input": SECRET,
                }
            ]
        )

    monkeypatch.setattr(APIRoute, "get_route_handler", lambda route: refuse_quoting_the_body)
    handle = SecretBodyRoute("/", lambda: None).get_route_handler()
    with pytest.raises(RequestValidationError) as refused:
        asyncio.run(handle(Request({"type": "http"})))
    assert (refused.value.__cause__, refused.value.__context__) == (None, None)
    assert SECRET not in repr(refused.value.errors())


def test_no_response_of_any_route_holds_a_stored_value(client, environ):
    responses = [
        client.put(GITHUB, json={"value": SECRET}),
        client.get(URL),
        client.put(f"{URL}/gitlab_token", json={"value": SECRET}),
        client.put(GITHUB, json={"value": SECRET}),
        client.put(f"{URL}/slack_token", json={"value": SECRET}),
        client.put(GITHUB, json={"value": SECRET, "extra": 1}),
    ]
    environ["GITHUB_TOKEN"] = SECRET
    responses += [
        client.get(URL),
        client.put(GITHUB, json={"value": SECRET}),
        client.delete(GITHUB),
        client.get(URL),
    ]
    assert [SECRET in response.text for response in responses] == [False] * len(responses)


def test_no_log_line_holds_the_value(client, caplog):
    caplog.set_level(logging.DEBUG)
    client.put(GITHUB, json={"value": SECRET})
    client.put(GITHUB, json={"value": [SECRET]})
    client.put(GITHUB, json={"value": f"{SECRET} {SECRET}"})
    assert SECRET not in caplog.text


def test_request_model_does_not_show_the_value_when_printed():
    body = SetCredentialRequest.model_validate({"value": SECRET})
    assert SECRET not in repr(body)
    assert SECRET not in str(body.model_dump())


# ----------------------------------------------------------------- remove
def test_remove_deletes_the_stored_value(client, session):
    client.put(GITHUB, json={"value": SECRET})
    response = client.delete(GITHUB)
    assert (response.status_code, response.content) == (204, b"")
    assert sealed(session) is None
    assert status_of(client, "github_token") == NOT_SET[1]


def test_remove_of_a_credential_with_no_stored_value_succeeds(client):
    assert client.delete(GITHUB).status_code == 204
    assert client.delete(GITHUB).status_code == 204


def test_remove_leaves_the_other_credentials_stored(client, session):
    client.put(GITHUB, json={"value": SECRET})
    client.put(f"{URL}/gitlab_token", json={"value": OTHER_SECRET})
    client.delete(GITHUB)
    assert sealed(session, "gitlab_token") is not None


def test_remove_is_allowed_while_the_environment_sets_the_credential(client, session, environ):
    client.put(GITHUB, json={"value": SECRET})
    environ["GITHUB_TOKEN"] = OTHER_SECRET
    assert client.delete(GITHUB).status_code == 204
    assert sealed(session) is None
    assert status_of(client, "github_token")["source"] == "environment"


def test_remove_of_an_unknown_credential_is_not_found(client):
    response = client.delete(f"{URL}/slack_token")
    assert (response.status_code, response.json()["code"]) == (404, "credential_not_found")


def test_remove_takes_no_request_body(client, session):
    client.put(GITHUB, json={"value": SECRET})
    response = client.request("DELETE", GITHUB, json={"value": SECRET})
    assert response.status_code == 422
    assert SECRET not in response.text
    assert sealed(session) is not None


# ------------------------------------------------------------------ who may
ROUTES = [("GET", URL), ("PUT", GITHUB), ("DELETE", GITHUB)]


@pytest.mark.parametrize(("method", "url"), ROUTES)
def test_request_from_another_machine_is_refused(app, session, method, url):
    remote = TestClient(app, base_url="http://localhost:8000", client=("203.0.113.7", 50000))
    body = {"value": SECRET} if method == "PUT" else None
    response = remote.request(method, url, json=body)
    assert (response.status_code, response.json()["code"]) == (403, "not_local_request")
    assert sealed(session) is None


@pytest.mark.parametrize(("method", "url"), ROUTES)
def test_request_from_a_web_page_of_another_site_is_refused(client, session, method, url):
    body = {"value": SECRET} if method == "PUT" else None
    response = client.request(method, url, headers={"Origin": "https://evil.example"}, json=body)
    assert (response.status_code, response.json()["code"]) == (403, "not_local_request")
    assert sealed(session) is None


def test_stored_value_survives_a_removal_that_was_refused(app, client, session):
    client.put(GITHUB, json={"value": SECRET})
    remote = TestClient(app, base_url="http://localhost:8000", client=("203.0.113.7", 50000))
    assert remote.delete(GITHUB).status_code == 403
    assert sealed(session) is not None
