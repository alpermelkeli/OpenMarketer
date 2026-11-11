"""Tests for the connection attempt store. They need PostgreSQL (see the root ``conftest.py``)."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.credentials.catalog import SealedValue
from openmarketer_core.db.models import SocialConnectionAttemptRecord, Workspace
from openmarketer_core.db.projects import create_project
from openmarketer_core.db.social_connection_attempts import (
    TakenAttempt,
    store_connection_attempt,
    take_connection_attempt,
)
from openmarketer_core.social_accounts.catalog import SocialPlatform

SECRET = SealedValue(nonce=b"attempt-nonc", ciphertext=b"request token secret")
LIFETIME = timedelta(minutes=15)


def new_workspace(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


@pytest.fixture
def workspace_id(session) -> uuid.UUID:
    return new_workspace(session)


@pytest.fixture
def project_id(session, workspace_id) -> uuid.UUID:
    url = "https://github.com/acme/app"
    return create_project(session, workspace_id=workspace_id, name="app", source_repo_url=url).id


def store(session, workspace_id, project_id, state="request-token", lifetime=LIFETIME):
    store_connection_attempt(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        platform=SocialPlatform.X,
        state=state,
        secret=SECRET,
        lifetime=lifetime,
    )


def attempts(session) -> int:
    return session.scalar(select(func.count()).select_from(SocialConnectionAttemptRecord)) or 0


def test_attempt_is_taken_with_its_project_and_secret(session, workspace_id, project_id):
    store(session, workspace_id, project_id)
    taken = take_connection_attempt(session, workspace_id=workspace_id, state="request-token")
    assert taken == TakenAttempt(
        project_id=project_id, platform=SocialPlatform.X, secret=SECRET, expired=False
    )


def test_attempt_is_taken_once(session, workspace_id, project_id):
    store(session, workspace_id, project_id)
    take_connection_attempt(session, workspace_id=workspace_id, state="request-token")
    again = take_connection_attempt(session, workspace_id=workspace_id, state="request-token")
    assert again is None
    assert attempts(session) == 0


def test_attempt_expires_after_its_lifetime_by_the_databases_clock(
    session, workspace_id, project_id
):
    store(session, workspace_id, project_id)
    expires_at = session.scalar(select(SocialConnectionAttemptRecord.expires_at))
    assert expires_at == session.scalar(select(func.now())) + LIFETIME
    session.execute(text("UPDATE social_connection_attempt SET expires_at = now()"))
    taken = take_connection_attempt(session, workspace_id=workspace_id, state="request-token")
    assert taken is not None and taken.expired


def test_attempt_of_another_workspace_is_not_found_and_stays(session, workspace_id, project_id):
    store(session, workspace_id, project_id)
    other = new_workspace(session)
    assert take_connection_attempt(session, workspace_id=other, state="request-token") is None
    assert attempts(session) == 1


def test_storing_an_attempt_drops_the_workspaces_expired_ones(session, workspace_id, project_id):
    store(session, workspace_id, project_id, state="old")
    session.execute(text("UPDATE social_connection_attempt SET expires_at = now()"))
    store(session, workspace_id, project_id, state="new")
    states = session.scalars(select(SocialConnectionAttemptRecord.state)).all()
    assert states == ["new"]


def test_storing_an_attempt_keeps_another_workspaces_expired_ones(
    session, workspace_id, project_id
):
    store(session, workspace_id, project_id, state="theirs")
    session.execute(text("UPDATE social_connection_attempt SET expires_at = now()"))
    other = new_workspace(session)
    url = "https://github.com/other/app"
    other_project = create_project(session, workspace_id=other, name="o", source_repo_url=url).id
    store(session, other, other_project, state="ours")
    assert attempts(session) == 2


def test_state_is_stored_once(session, workspace_id, project_id):
    store(session, workspace_id, project_id)
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            store(session, workspace_id, project_id)


def test_storing_and_taking_wait_for_other_writes_in_the_workspace_first(
    session, workspace_id, project_id, statements
):
    statements.clear()
    store(session, workspace_id, project_id)
    assert "FOR UPDATE" in statements[0]
    statements.clear()
    take_connection_attempt(session, workspace_id=workspace_id, state="request-token")
    assert "FOR UPDATE" in statements[0]


def test_taken_attempt_does_not_show_its_secret(session, workspace_id, project_id):
    store(session, workspace_id, project_id)
    taken = take_connection_attempt(session, workspace_id=workspace_id, state="request-token")
    assert repr(SECRET.ciphertext) not in repr(taken)
