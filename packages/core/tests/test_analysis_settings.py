"""Tests for the limits a workspace's analyses run under. They need PostgreSQL."""

import uuid

import pytest
from sqlalchemy.orm import Session

from openmarketer_core.db.models import Workspace
from openmarketer_core.db.workspace_settings import store_setting
from openmarketer_core.repository_analysis.analyzer_agent.rules import Limits
from openmarketer_core.workspace_settings.analysis_limits import (
    AnalysisLimitsChoice,
    AnalysisLimitsRefused,
)
from openmarketer_core.workspace_settings.analysis_settings import (
    StoredAnalysisLimitsInvalid,
    analysis_limits,
    analysis_limits_setting,
    remove_analysis_limits,
    set_analysis_limits,
)

OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")


@pytest.fixture
def workspace_id(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


def set_limits(session, workspace_id, **chosen):
    return set_analysis_limits(
        session, workspace_id=workspace_id, choice=AnalysisLimitsChoice(**chosen), set_by=OWNER
    )


def test_without_a_choice_the_analyzers_defaults_apply(session, workspace_id):
    setting = analysis_limits_setting(session, workspace_id=workspace_id)
    assert setting.limits == setting.defaults == Limits()
    assert setting.choice is None and setting.set_by is None


def test_chosen_limits_apply_and_the_rest_keep_their_default(session, workspace_id):
    setting = set_limits(session, workspace_id, max_steps=80)
    assert setting.limits == Limits(max_steps=80)
    assert setting.set_by == OWNER
    assert analysis_limits(session, workspace_id=workspace_id) == Limits(max_steps=80)


def test_limits_out_of_bounds_are_refused(session, workspace_id):
    with pytest.raises(AnalysisLimitsRefused):
        set_limits(session, workspace_id, max_cost_usd=500)


def test_removing_the_choice_brings_back_the_defaults(session, workspace_id):
    set_limits(session, workspace_id, max_cost_usd=2.0)
    assert remove_analysis_limits(session, workspace_id=workspace_id).limits == Limits()


def test_empty_choice_removes_the_stored_one(session, workspace_id):
    set_limits(session, workspace_id, max_steps=80)
    assert set_limits(session, workspace_id).choice is None


def test_stored_limits_that_break_a_rule_say_what_to_reset(session, workspace_id):
    store_setting(
        session,
        workspace_id=workspace_id,
        name="analysis.limits",
        value={"max_steps": 0},
        set_by=OWNER,
    )
    with pytest.raises(StoredAnalysisLimitsInvalid, match="Settings, Analysis"):
        analysis_limits(session, workspace_id=workspace_id)
    setting = analysis_limits_setting(session, workspace_id=workspace_id)
    assert setting.unreadable and setting.limits == Limits()
