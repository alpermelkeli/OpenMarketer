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


async def status_of(app, headers: list[tuple[bytes, bytes]]) -> int:
    """Send a request from this machine with exactly these headers, which no HTTP client allows."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": RUN_URL,
        "raw_path": RUN_URL.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
    statuses: list[int] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    await app(scope, receive, send)
    return statuses[0]


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:8000", "LOCALHOST:8000", "127.0.0.1:8000", "[::1]:8000"]
)
async def test_request_addressed_to_this_machine_by_name_reaches_the_route(app, host):
    assert await status_of(app, [(b"host", host.encode())]) == 404


async def test_request_without_a_host_header_is_refused(app):
    assert await status_of(app, []) == 403


@pytest.mark.parametrize(
    "host",
    [
        "",
        "evil.example",
        "localhost.evil.example",
        "localhost@evil.example",
        "evil.example@localhost",
        "localhost#.evil.example",
        "localhost/.evil.example",
        "localhost\\.evil.example",
        "localhost:8000@evil.example",
        "localhost:port",
        "localhost:",
        "localhost :8000",
        " localhost",
        "127.0.0.1.evil.example",
        "127.0.0.2",
        "::1",
        "[::1",
        "http://localhost",
    ],
)
async def test_host_header_that_is_not_exactly_a_local_name_is_refused(app, host):
    assert await status_of(app, [(b"host", host.encode())]) == 403


async def test_two_host_headers_are_refused(app):
    headers = [(b"host", b"localhost:8000"), (b"host", b"evil.example")]
    assert await status_of(app, headers) == 403
    assert await status_of(app, list(reversed(headers))) == 403


@pytest.mark.parametrize(
    "origin", ["http://localhost:3000", "http://127.0.0.1:3000", "http://[::1]:3000"]
)
async def test_local_origin_reaches_the_route(app, origin):
    headers = [(b"host", b"localhost:8000"), (b"origin", origin.encode())]
    assert await status_of(app, headers) == 404


@pytest.mark.parametrize(
    "origin",
    [
        "http://[::1",
        "null",
        "",
        "localhost",
        "http://localhost@evil.example",
        "http://evil.example#@localhost",
        "http://localhost.evil.example",
        "http://localhost/path",
        "file://localhost",
        "https://evil.example",
    ],
)
async def test_origin_that_is_not_exactly_a_local_one_is_refused(app, origin):
    headers = [(b"host", b"localhost:8000"), (b"origin", origin.encode())]
    assert await status_of(app, headers) == 403


async def test_one_foreign_origin_among_several_is_refused(app):
    headers = [
        (b"host", b"localhost:8000"),
        (b"origin", b"http://localhost:3000"),
        (b"origin", b"https://evil.example"),
    ]
    assert await status_of(app, headers) == 403
