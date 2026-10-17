"""Tests for the caps on a workspace's spending for generated media: the rule of a valid
choice, and where each cap comes from. The resolution tests need PostgreSQL."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from openmarketer_core.budget.caps import CreativeCaps
from openmarketer_core.db.models import Workspace
from openmarketer_core.db.workspace_settings import store_setting
from openmarketer_core.workspace_settings.creative_budget import (
    DEFAULT_CAPS,
    CreativeBudgetChoice,
    CreativeBudgetRefused,
    acceptable_budget,
    budget_from_stored,
)
from openmarketer_core.workspace_settings.creative_budget_settings import (
    CREATIVE_BUDGET_SETTING,
    CapSource,
    CreativeBudgetEnvironmentInvalid,
    CreativeBudgetSetByEnvironment,
    StoredCreativeBudgetInvalid,
    creative_budget_setting,
    creative_caps,
    remove_creative_budget,
    set_creative_budget,
)

OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
NO_ENVIRONMENT: dict[str, str] = {}


@pytest.fixture
def workspace_id(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


def choose(session, workspace_id, environ=NO_ENVIRONMENT, **chosen):
    return set_creative_budget(
        session,
        environ,
        workspace_id=workspace_id,
        choice=CreativeBudgetChoice(**chosen),
        set_by=OWNER,
    )


# ------------------------------------------------------------------- rule
def test_the_defaults_are_sixty_dollars_a_month_and_a_quarter_for_one_action():
    assert DEFAULT_CAPS == CreativeCaps(Decimal(60), Decimal("0.25"))


def test_a_cap_of_zero_is_a_valid_choice():
    choice = acceptable_budget(CreativeBudgetChoice(monthly_usd=Decimal(0)))
    assert choice.monthly_usd == Decimal(0)


@pytest.mark.parametrize(
    ("field", "amount"),
    [
        ("monthly_usd", Decimal(-1)),
        ("monthly_usd", Decimal(10_001)),
        ("auto_per_action_usd", Decimal(101)),
        ("auto_per_action_usd", Decimal("NaN")),
    ],
)
def test_a_cap_out_of_bounds_is_refused(field, amount):
    with pytest.raises(CreativeBudgetRefused) as refused:
        acceptable_budget(CreativeBudgetChoice(**{field: amount}))
    assert refused.value.field == field


def test_a_choice_reads_back_exactly_as_it_was_stored():
    choice = acceptable_budget(CreativeBudgetChoice(monthly_usd=Decimal("12.345678")))
    assert budget_from_stored(choice.as_stored()) == choice


def test_a_stored_value_that_is_not_a_cap_is_refused():
    with pytest.raises(CreativeBudgetRefused):
        budget_from_stored({"daily_usd": "5"})


# ------------------------------------------------------------- resolution
def test_without_a_choice_or_the_environment_the_defaults_apply(session, workspace_id):
    setting = creative_budget_setting(session, NO_ENVIRONMENT, workspace_id=workspace_id)

    assert setting.caps == DEFAULT_CAPS
    assert setting.monthly.source is setting.auto_per_action.source is CapSource.DEFAULT
    assert setting.choice is None


def test_a_chosen_cap_applies_and_the_other_keeps_its_default(session, workspace_id):
    setting = choose(session, workspace_id, monthly_usd=Decimal(20))

    assert setting.caps == CreativeCaps(Decimal(20), DEFAULT_CAPS.auto_per_action_usd)
    assert setting.monthly.source is CapSource.STORED
    assert setting.set_by == OWNER


def test_the_environment_wins_over_a_stored_choice(session, workspace_id):
    choose(session, workspace_id, monthly_usd=Decimal(20), auto_per_action_usd=Decimal("0.10"))
    environ = {"MEDIA_MONTHLY_CAP_USD": " 45 ", "MEDIA_AUTO_APPROVE_USD": ""}

    setting = creative_budget_setting(session, environ, workspace_id=workspace_id)

    assert setting.caps == CreativeCaps(Decimal(45), Decimal("0.10"))
    assert setting.monthly.source is CapSource.ENVIRONMENT
    assert setting.auto_per_action.source is CapSource.STORED


def test_choosing_a_cap_the_environment_sets_is_refused(session, workspace_id):
    with pytest.raises(CreativeBudgetSetByEnvironment, match="MEDIA_AUTO_APPROVE_USD"):
        choose(
            session,
            workspace_id,
            {"MEDIA_AUTO_APPROVE_USD": "1"},
            auto_per_action_usd=Decimal("0.5"),
        )


def test_an_invalid_environment_value_names_its_variable(session, workspace_id):
    with pytest.raises(CreativeBudgetEnvironmentInvalid, match="MEDIA_MONTHLY_CAP_USD"):
        creative_caps(session, {"MEDIA_MONTHLY_CAP_USD": "sixty"}, workspace_id=workspace_id)


def test_removing_the_choice_brings_back_the_defaults(session, workspace_id):
    choose(session, workspace_id, monthly_usd=Decimal(20))

    setting = remove_creative_budget(session, NO_ENVIRONMENT, workspace_id=workspace_id)

    assert setting.caps == DEFAULT_CAPS


def test_an_empty_choice_removes_the_stored_one(session, workspace_id):
    choose(session, workspace_id, monthly_usd=Decimal(20))

    assert choose(session, workspace_id).choice is None


def test_a_stored_choice_that_breaks_a_rule_refuses_spending_until_reset(session, workspace_id):
    store_setting(
        session,
        workspace_id=workspace_id,
        name=CREATIVE_BUDGET_SETTING,
        value={"monthly_usd": "-5"},
        set_by=OWNER,
    )

    setting = creative_budget_setting(session, NO_ENVIRONMENT, workspace_id=workspace_id)
    assert setting.unreadable and setting.caps == DEFAULT_CAPS
    with pytest.raises(StoredCreativeBudgetInvalid):
        creative_caps(session, NO_ENVIRONMENT, workspace_id=workspace_id)
