"""The limits of the workspace's analyses: reading them, choosing them, going back to the defaults.

A limit the workspace does not choose keeps the analyzer's default. The bounds
and the rule are core's (``workspace_settings/analysis_limits.py``); a run reads
the limits at each attempt, so a change applies from a run's next attempt.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from openmarketer_api.dependencies import DbSession, no_request_body
from openmarketer_api.identity import CurrentUserId, CurrentWorkspaceId
from openmarketer_core.workspace_settings.analysis_limits import (
    MAX_COST_USD,
    MAX_STEPS,
    MIN_COST_USD,
    MIN_STEPS,
    AnalysisLimitsChoice,
)
from openmarketer_core.workspace_settings.analysis_settings import (
    AnalysisLimitsSetting,
    analysis_limits_setting,
    remove_analysis_limits,
    set_analysis_limits,
)

router = APIRouter(prefix="/settings/analysis", tags=["settings"])


class AnalysisLimitsRequest(BaseModel):
    """The limits the workspace chooses. A limit left out or null keeps the default."""

    model_config = ConfigDict(extra="forbid")

    max_steps: int | None = Field(default=None, description=f"From {MIN_STEPS} to {MAX_STEPS}.")
    max_cost_usd: float | None = Field(
        default=None, description=f"From {MIN_COST_USD:g} to {MAX_COST_USD:g} US dollars."
    )


class AnalysisLimitsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(description="Turns the agent may take in one run.")
    max_cost_usd: float = Field(description="What its model calls may cost in one run, in USD.")


class ChosenAnalysisLimitsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int | None
    max_cost_usd: float | None


class AnalysisLimitsBoundsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_steps: int
    max_steps: int
    min_cost_usd: float
    max_cost_usd: float


class AnalysisSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limits: AnalysisLimitsResponse = Field(description="The limits a run gets now.")
    defaults: AnalysisLimitsResponse = Field(description="The analyzer's own limits.")
    chosen: ChosenAnalysisLimitsResponse | None
    set_at: datetime | None
    set_by: uuid.UUID | None
    unreadable: bool = Field(
        description=(
            "A stored choice breaks a rule: the defaults are shown, and runs are refused"
            " until it is reset."
        )
    )
    bounds: AnalysisLimitsBoundsResponse


BOUNDS = AnalysisLimitsBoundsResponse(
    min_steps=MIN_STEPS, max_steps=MAX_STEPS, min_cost_usd=MIN_COST_USD, max_cost_usd=MAX_COST_USD
)


def _response(setting: AnalysisLimitsSetting) -> AnalysisSettingsResponse:
    choice = setting.choice
    limits, defaults = setting.limits, setting.defaults
    return AnalysisSettingsResponse(
        limits=AnalysisLimitsResponse(max_steps=limits.max_steps, max_cost_usd=limits.max_cost_usd),
        defaults=AnalysisLimitsResponse(
            max_steps=defaults.max_steps, max_cost_usd=defaults.max_cost_usd
        ),
        chosen=None
        if choice is None
        else ChosenAnalysisLimitsResponse(
            max_steps=choice.max_steps, max_cost_usd=choice.max_cost_usd
        ),
        set_at=setting.set_at,
        set_by=setting.set_by,
        unreadable=setting.unreadable,
        bounds=BOUNDS,
    )


@router.get("", operation_id="getAnalysisSettings", response_model=AnalysisSettingsResponse)
def get_settings(session: DbSession, workspace_id: CurrentWorkspaceId) -> AnalysisSettingsResponse:
    """The limits a run of an analysis gets, the defaults, and what the workspace chose."""
    return _response(analysis_limits_setting(session, workspace_id=workspace_id))


@router.put("", operation_id="setAnalysisLimits", response_model=AnalysisSettingsResponse)
def set_limits(
    body: AnalysisLimitsRequest,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    user: CurrentUserId,
) -> AnalysisSettingsResponse:
    """Choose the limits, in place of the choice before; none chosen goes back to the defaults."""
    choice = AnalysisLimitsChoice(max_steps=body.max_steps, max_cost_usd=body.max_cost_usd)
    setting = set_analysis_limits(session, workspace_id=workspace_id, choice=choice, set_by=user)
    return _response(setting)


@router.delete(
    "",
    operation_id="removeAnalysisLimits",
    status_code=204,
    dependencies=[Depends(no_request_body)],
)
def remove_limits(session: DbSession, workspace_id: CurrentWorkspaceId) -> None:
    """Go back to the analyzer's defaults."""
    remove_analysis_limits(session, workspace_id=workspace_id)
