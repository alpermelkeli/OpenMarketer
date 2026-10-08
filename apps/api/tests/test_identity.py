"""Tests for who a request is taken to be in single-user local mode.

Any workspace route would do; these ask for the status of a run that does not
exist, so a request that is let through is answered 404 by the route. They
need PostgreSQL (see the root ``conftest.py``).
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from openmarketer_api.dependencies import db_session

RUN_URL = f"/v1/projects/{uuid.uuid4()}/analyses/{uuid.uuid4()}"
NOT_LOCAL = {
    "code": "not_local_request",
    "message": "without a login the API serves only requests made on the machine it runs on",
}


@pytest.fixture(autouse=True)
def database(app, session) -> None:
    """Requests work in the test's session, which is rolled back afterwards."""
    app.dependency_overrides[db_session] = lambda: session


def test_request_made_on_this_machine_reaches_the_route(client):
    response = client.get(RUN_URL)
    assert (response.status_code, response.json()["code"]) == (404, "analysis_not_found")


def test_request_from_the_local_dashboard_reaches_the_route(client):
    response = client.get(RUN_URL, headers={"Origin": "http://localhost:3000"})
    assert (response.status_code, response.json()["code"]) == (404, "analysis_not_found")


def test_request_from_another_machine_is_refused(app):
    remote = TestClient(app, base_url="http://localhost:8000", client=("203.0.113.7", 50000))
    response = remote.get(RUN_URL)
    assert (response.status_code, response.json()) == (403, NOT_LOCAL)


def test_request_from_a_web_page_of_another_site_is_refused(client):
    response = client.get(RUN_URL, headers={"Origin": "https://evil.example"})
    assert (response.status_code, response.json()) == (403, NOT_LOCAL)


def test_request_addressed_to_a_foreign_host_name_is_refused(app):
    rebound = TestClient(app, base_url="http://evil.example:8000", client=("127.0.0.1", 50000))
    assert rebound.get(RUN_URL).status_code == 403
