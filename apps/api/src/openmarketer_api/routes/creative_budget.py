"""The workspace's budget for generated media: reading it, choosing caps, going back to defaults.

Each cap comes from the environment, then the workspace's choice, then the default; the
order, the bounds and the rule are core's (``workspace_settings/creative_budget_settings.py``).
A cap the environment sets cannot be chosen here. What was spent and is held this calendar
month (UTC) is read from the ledger (``budget/ledger.py``) against the monthly cap in use.
Nothing here spends, reserves or releases money.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from openmarketer_api.dependencies import DbSession, Environment, no_request_body
from openmarketer_api.errors import problems
from openmarketer_api.identity import CurrentUserId, CurrentWorkspaceId
from openmarketer_core.budget.ledger import spent_this_month
from openmarketer_core.workspace_settings.creative_budget import (
    MAX_AUTO_PER_ACTION_USD,
    MAX_MONTHLY_USD,
    CreativeBudgetChoice,
)
from openmarketer_core.workspace_settings.creative_budget_settings import (
    CapSetting,
    CapSource,
    CreativeBudgetSetting,
    creative_budget_setting,
    remove_creative_budget,
    set_creative_budget,
)

router = APIRouter(prefix="/settings/creative-budget", tags=["settings"])


class CreativeBudgetRequest(BaseModel):
    """The caps the workspace chooses. A cap left out or null keeps its environment or default."""

    model_config = ConfigDict(extra="forbid")

    monthly_usd: float | None = Field(
        default=None,
        allow_inf_nan=False,
        description=(
            f"What the month's generated media may cost, from 0 to {MAX_MONTHLY_USD:,} USD."
        ),
    )
    auto_per_action_usd: float | None = Field(
        default=None,
        allow_inf_nan=False,
        description=(
            "The most one action may spend without a person's approval, from 0 to"
            f" {MAX_AUTO_PER_ACTION_USD:,} USD."
        ),
    )


class CapResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value_usd: float = Field(description="The cap in use.")
    source: CapSource = Field(description="Where it comes from: environment, stored or default.")
    default_usd: float
    environment_variable: str = Field(description="The variable that would set it instead.")


class ChosenCreativeBudgetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    monthly_usd: float | None
    auto_per_action_usd: float | None


class MonthlySpendResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    month_start: datetime = Field(description="The start of this calendar month, in UTC.")
    spent_usd: float
    reserved_usd: float = Field(description="Held for actions that have not settled yet.")
    cap_usd: float
    remaining_usd: float = Field(description="What can still be reserved; never below zero.")


class CreativeBudgetBoundsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_monthly_usd: float
    max_auto_per_action_usd: float


class CreativeBudgetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    monthly: CapResponse
    auto_per_action: CapResponse
    chosen: ChosenCreativeBudgetResponse | None
    set_at: datetime | None
    set_by: uuid.UUID | None
    unreadable: bool = Field(
        description=(
            "A stored choice breaks a rule: the other sources are shown, and spending is"
            " refused until it is reset."
        )
    )
    spent_this_month: MonthlySpendResponse
    bounds: CreativeBudgetBoundsResponse


BOUNDS = CreativeBudgetBoundsResponse(
    max_monthly_usd=float(MAX_MONTHLY_USD), max_auto_per_action_usd=float(MAX_AUTO_PER_ACTION_USD)
)


def _cap(cap: CapSetting) -> CapResponse:
    return CapResponse(
        value_usd=float(cap.value),
        source=cap.source,
        default_usd=float(cap.default),
        environment_variable=cap.environment_variable,
    )


def _optional(amount: Decimal | None) -> float | None:
    return None if amount is None else float(amount)


def _response(
    session: DbSession, workspace_id: uuid.UUID, setting: CreativeBudgetSetting
) -> CreativeBudgetResponse:
    choice = setting.choice
    month = spent_this_month(session, workspace_id=workspace_id, caps=setting.caps)
    return CreativeBudgetResponse(
        monthly=_cap(setting.monthly),
        auto_per_action=_cap(setting.auto_per_action),
        chosen=None
        if choice is None
        else ChosenCreativeBudgetResponse(
            monthly_usd=_optional(choice.monthly_usd),
            auto_per_action_usd=_optional(choice.auto_per_action_usd),
        ),
        set_at=setting.set_at,
        set_by=setting.set_by,
        unreadable=setting.unreadable,
        spent_this_month=MonthlySpendResponse(
            # The database gives the instant in UTC without a zone.
            month_start=month.month_start.replace(tzinfo=month.month_start.tzinfo or UTC),
            spent_usd=float(month.spent_usd),
            reserved_usd=float(month.reserved_usd),
            cap_usd=float(month.cap_usd),
            remaining_usd=float(month.remaining_usd),
        ),
        bounds=BOUNDS,
    )


def _amount(value: float | None) -> Decimal | None:
    # Through its shortest text, so that 0.1 is a tenth of a dollar, as the ledger reads it.
    return None if value is None else Decimal(str(value))


@router.get(
    "",
    operation_id="getCreativeBudget",
    response_model=CreativeBudgetResponse,
    responses=problems(409),
)
def get_budget(
    session: DbSession, environ: Environment, workspace_id: CurrentWorkspaceId
) -> CreativeBudgetResponse:
    """The caps in use with where each comes from, what was chosen, and this month's spending.

    409 `environment_setting_invalid` when an environment variable sets a cap to a value that
    breaks its rule.
    """
    setting = creative_budget_setting(session, environ, workspace_id=workspace_id)
    return _response(session, workspace_id, setting)


@router.put(
    "",
    operation_id="setCreativeBudget",
    response_model=CreativeBudgetResponse,
    responses=problems(409),
)
def set_budget(
    body: CreativeBudgetRequest,
    session: DbSession,
    environ: Environment,
    workspace_id: CurrentWorkspaceId,
    user: CurrentUserId,
) -> CreativeBudgetResponse:
    """Choose the caps, in place of the choice before; none chosen removes the choice.

    A cap out of its bounds is a 422 beside its field. 409 `cap_set_by_environment` for a cap
    an environment variable sets.
    """
    choice = CreativeBudgetChoice(
        monthly_usd=_amount(body.monthly_usd),
        auto_per_action_usd=_amount(body.auto_per_action_usd),
    )
    setting = set_creative_budget(
        session, environ, workspace_id=workspace_id, choice=choice, set_by=user
    )
    return _response(session, workspace_id, setting)


@router.delete(
    "",
    operation_id="removeCreativeBudget",
    status_code=204,
    responses=problems(409),
    dependencies=[Depends(no_request_body)],
)
def remove_budget(
    session: DbSession, environ: Environment, workspace_id: CurrentWorkspaceId
) -> None:
    """Go back to the environment's caps and the defaults."""
    remove_creative_budget(session, environ, workspace_id=workspace_id)
