"""Tests for the cursors of the list routes: positions written as text and read back."""

import base64
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.exceptions import RequestValidationError

from openmarketer_api.dependencies import db_session
from openmarketer_api.pagination import (
    created_before,
    created_cursor,
    version_before,
    version_cursor,
)
from openmarketer_core.db.pages import CreatedPosition

POSITION = CreatedPosition(
    datetime(2026, 10, 1, 12, 30, 15, 123456, tzinfo=UTC),
    uuid.UUID("22222222-2222-4222-8222-222222222222"),
)


def encoded(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_position_survives_being_written_as_a_cursor():
    assert created_before(created_cursor(POSITION)) == POSITION


def test_position_in_another_time_zone_is_the_same_moment():
    kept = created_before(
        created_cursor(
            CreatedPosition(
                POSITION.created_at.astimezone(timezone(timedelta(hours=3))), POSITION.id
            )
        )
    )
    assert kept == POSITION


def test_cursor_is_safe_in_a_query_string_without_escaping():
    cursor = created_cursor(POSITION)
    assert cursor is not None
    assert cursor.replace("-", "").replace("_", "").isalnum()


def test_end_of_a_list_has_no_cursor():
    assert (created_cursor(None), version_cursor(None)) == (None, None)


def test_request_without_a_cursor_starts_at_the_newest():
    assert (created_before(None), version_before(None)) == (None, None)


def test_version_survives_being_written_as_a_cursor():
    assert version_before(version_cursor(41)) == 41


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "not base64 !",
        encoded("no separator"),
        encoded(f"yesterday|{POSITION.id}"),
        encoded("2026-10-01T12:30:15+00:00|not-a-uuid"),
        encoded(f"2026-10-01T12:30:15|{POSITION.id}"),
        "\u00e9",
        "41",
    ],
)
def test_text_that_is_no_position_is_not_read_as_one(cursor):
    with pytest.raises(RequestValidationError):
        created_before(cursor)


@pytest.mark.parametrize("url", ["/v1/projects", f"/v1/projects/{uuid.uuid4()}/analyses"])
def test_list_answers_a_cursor_it_did_not_write_with_a_validation_error(app, client, session, url):
    app.dependency_overrides[db_session] = lambda: session
    response = client.get(url, params={"cursor": "41"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "cursor"]


def test_cursor_longer_than_any_the_api_writes_is_rejected(app, client, session):
    app.dependency_overrides[db_session] = lambda: session
    assert client.get("/v1/projects", params={"cursor": "A" * 201}).status_code == 422
