"""Tests for the workspace settings store. They need PostgreSQL (see the root ``conftest.py``)."""

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from openmarketer_core.db.models import Workspace
from openmarketer_core.db.workspace_settings import (
    remove_setting,
    store_setting,
    stored_setting,
    stored_settings,
)

OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
COLLEAGUE = uuid.UUID("22222222-2222-4222-8222-222222222222")


def new_workspace(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


@pytest.fixture
def workspace_id(session) -> uuid.UUID:
    return new_workspace(session)


def store(session, workspace_id, name="model.tier.fast", value=None, set_by=OWNER):
    return store_setting(
        session,
        workspace_id=workspace_id,
        name=name,
        value=value or {"model": "a/b"},
        set_by=set_by,
    )


def test_stored_value_is_read_back_with_who_stored_it_and_when(session, workspace_id):
    stored = store(session, workspace_id)
    assert stored.value == {"model": "a/b"}
    assert stored.set_by == OWNER
    assert stored.set_at == session.scalar(select(func.now()))
    assert stored_setting(session, workspace_id=workspace_id, name="model.tier.fast") == stored


def test_storing_again_replaces_the_value(session, workspace_id):
    store(session, workspace_id)
    store(session, workspace_id, value={"model": "c/d"}, set_by=COLLEAGUE)
    found = stored_setting(session, workspace_id=workspace_id, name="model.tier.fast")
    assert found is not None
    assert (found.value, found.set_by) == ({"model": "c/d"}, COLLEAGUE)


def test_settings_are_listed_by_prefix_in_name_order(session, workspace_id):
    store(session, workspace_id, name="model.tier.strong")
    store(session, workspace_id, name="model.role.writer", value={"temperature": 0.2})
    store(session, workspace_id, name="analysis.limits", value={"max_steps": 9})
    names = [s.name for s in stored_settings(session, workspace_id=workspace_id, prefix="model.")]
    assert names == ["model.role.writer", "model.tier.strong"]


def test_prefix_is_matched_literally(session, workspace_id):
    store(session, workspace_id, name="modelXtier")
    assert stored_settings(session, workspace_id=workspace_id, prefix="model.") == []
    assert stored_settings(session, workspace_id=workspace_id, prefix="model_%") == []


def test_settings_of_another_workspace_are_not_seen(session, workspace_id):
    store(session, new_workspace(session))
    assert stored_settings(session, workspace_id=workspace_id, prefix="") == []
    assert stored_setting(session, workspace_id=workspace_id, name="model.tier.fast") is None


def test_removing_says_whether_there_was_a_value(session, workspace_id):
    store(session, workspace_id)
    assert remove_setting(session, workspace_id=workspace_id, name="model.tier.fast")
    assert not remove_setting(session, workspace_id=workspace_id, name="model.tier.fast")
    assert stored_setting(session, workspace_id=workspace_id, name="model.tier.fast") is None
