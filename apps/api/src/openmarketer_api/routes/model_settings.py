"""The workspace's models: what each tier and role resolves to, choosing them, and checking them.

A choice made here is laid over the configuration file (``config/models.yaml``)
for the workspace's runs; an environment variable still wins over it, and a
choice it would override is refused. Removing a choice goes back to the file.
What a valid choice is, and where each value comes from, is core's
(``workspace_settings/``); these routes translate.

The provider's catalogue of models is read through the API, not by the browser,
so the dashboard needs no access to the internet of its own; when it cannot be
read, the picker and the check say so and a model can still be typed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field

from openmarketer_api.dependencies import Catalog, DbSession, Models, no_request_body
from openmarketer_api.errors import problems
from openmarketer_api.identity import CurrentUserId, CurrentWorkspaceId
from openmarketer_core.llm_config import (
    REJECTABLE_PARAMETERS,
    CapabilityProblem,
    ModelRouter,
    RejectableParameter,
    Resolved,
)
from openmarketer_core.workspace_settings.model_choices import (
    MAX_FALLBACKS,
    MAX_MAX_TOKENS,
    MAX_TEMPERATURE,
    MIN_MAX_TOKENS,
    MIN_TEMPERATURE,
    ReasoningEffort,
    RoleChoice,
)
from openmarketer_core.workspace_settings.model_settings import (
    ChoiceKind,
    ModelSettings,
    RoleModelSource,
    RoleSetting,
    SettingChange,
    TierModelSource,
    TierSetting,
    model_problems,
    model_settings,
    remove_role_choice,
    remove_tier_model,
    set_rejected_parameters,
    set_role_choice,
    set_tier_model,
)

router = APIRouter(prefix="/settings/models", tags=["settings"])

# The form of a tier's and a role's name in the configuration file.
SettingKey = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


# ---------------------------------------------------------------- requests
class SetTierModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="The model, as provider/model.")


class RoleChoiceRequest(BaseModel):
    """What the workspace chooses for a role. A part left out or null is the file's."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    fallbacks: list[str] | None = Field(
        default=None, description=f"Tried in order when the model fails; at most {MAX_FALLBACKS}."
    )
    temperature: float | None = Field(
        default=None, description=f"From {MIN_TEMPERATURE:g} to {MAX_TEMPERATURE:g}."
    )
    max_tokens: int | None = Field(
        default=None, description=f"From {MIN_MAX_TOKENS} to {MAX_MAX_TOKENS}."
    )
    reasoning_effort: ReasoningEffort | None = None


class SetRejectedParametersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="The model, as provider/model.")
    parameters: list[RejectableParameter] = Field(
        description="What to leave out of a request to the model. Empty removes the mark."
    )


# --------------------------------------------------------------- responses
class SettingChangeResponse(BaseModel):
    """Who stored a choice and when."""

    model_config = ConfigDict(extra="forbid")

    set_at: datetime
    set_by: uuid.UUID


class TierSettingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: str
    model: str = Field(description="The model the tier resolves to now.")
    source: TierModelSource
    file_model: str = Field(description="The configuration file's model for the tier.")
    environment_variable: str = Field(description="The variable that sets the tier's model.")
    chosen: str | None = Field(description="The workspace's choice; null when it made none.")
    changed: SettingChangeResponse | None


class ResolvedModelResponse(BaseModel):
    """The model and parameters a role's request is made with."""

    model_config = ConfigDict(extra="forbid")

    model: str
    fallbacks: list[str]
    temperature: float
    max_tokens: int
    reasoning_effort: ReasoningEffort | None


class RoleChoiceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None
    fallbacks: list[str] | None
    temperature: float | None
    max_tokens: int | None
    reasoning_effort: ReasoningEffort | None


class RoleSettingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    kind: str = Field(
        description="`chat`, or `embeddings` or `decisions` for a role that takes a model only."
    )
    tier: str | None
    requires: list[str] = Field(description="What the role's model must support.")
    effective: ResolvedModelResponse = Field(description="What the role resolves to now.")
    model_source: RoleModelSource
    temperature_sent: bool = Field(
        description="Whether a request carries the temperature; not when its model rejects it."
    )
    inherited: ResolvedModelResponse = Field(
        description="What the role resolves to without the workspace's choice for it."
    )
    choice: RoleChoiceResponse | None
    changed: SettingChangeResponse | None
    environment_variable: str = Field(description="The variable that sets the role's model.")


class RejectedParametersResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    from_file: list[RejectableParameter]
    chosen: list[RejectableParameter]
    changed: SettingChangeResponse | None


class UnreadableChoiceResponse(BaseModel):
    """A stored choice that breaks a rule. Runs are refused until it is reset."""

    model_config = ConfigDict(extra="forbid")

    kind: ChoiceKind
    key: str = Field(description="The tier, role or model the choice is for.")


class ChoiceBoundsResponse(BaseModel):
    """What a role's choice may hold."""

    model_config = ConfigDict(extra="forbid")

    max_fallbacks: int
    min_temperature: float
    max_temperature: float
    min_max_tokens: int
    max_max_tokens: int
    rejectable_parameters: list[RejectableParameter]


class ModelSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tiers: list[TierSettingResponse]
    roles: list[RoleSettingResponse]
    rejected_parameters: list[RejectedParametersResponse]
    unreadable: list[UnreadableChoiceResponse]
    bounds: ChoiceBoundsResponse


class CatalogModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    context_length: int | None
    prompt_price: float | None = Field(description="US dollars per million input tokens.")
    completion_price: float | None = Field(description="US dollars per million output tokens.")
    supported_parameters: list[str]
    input_modalities: list[str]


class ModelCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool = Field(description="Whether the provider's catalogue could be read.")
    models: list[CatalogModelResponse]


class ModelProblemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    model: str
    problem: CapabilityProblem


class ModelCheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool = Field(description="Whether the provider's catalogue could be read.")
    problems: list[ModelProblemResponse]


BOUNDS = ChoiceBoundsResponse(
    max_fallbacks=MAX_FALLBACKS,
    min_temperature=MIN_TEMPERATURE,
    max_temperature=MAX_TEMPERATURE,
    min_max_tokens=MIN_MAX_TOKENS,
    max_max_tokens=MAX_MAX_TOKENS,
    rejectable_parameters=list(REJECTABLE_PARAMETERS),
)


def _change(change: SettingChange | None) -> SettingChangeResponse | None:
    if change is None:
        return None
    return SettingChangeResponse(set_at=change.set_at, set_by=change.set_by)


def _resolved(resolved: Resolved) -> ResolvedModelResponse:
    return ResolvedModelResponse(
        model=resolved.model,
        fallbacks=resolved.fallbacks,
        temperature=resolved.temperature,
        max_tokens=resolved.max_tokens,
        reasoning_effort=resolved.reasoning_effort,  # type: ignore[arg-type]
    )


def _tier(tier: TierSetting) -> TierSettingResponse:
    return TierSettingResponse(
        tier=tier.tier,
        model=tier.model,
        source=tier.source,
        file_model=tier.file_model,
        environment_variable=tier.environment_variable,
        chosen=tier.chosen,
        changed=_change(tier.changed),
    )


def _role(role: RoleSetting) -> RoleSettingResponse:
    choice = role.choice
    return RoleSettingResponse(
        role=role.role,
        kind=role.kind,
        tier=role.tier,
        requires=list(role.requires),
        effective=_resolved(role.effective),
        model_source=role.model_source,
        temperature_sent=role.temperature_sent,
        inherited=_resolved(role.inherited),
        choice=None
        if choice is None
        else RoleChoiceResponse(
            model=choice.model,
            fallbacks=None if choice.fallbacks is None else list(choice.fallbacks),
            temperature=choice.temperature,
            max_tokens=choice.max_tokens,
            reasoning_effort=choice.reasoning_effort,
        ),
        changed=_change(role.changed),
        environment_variable=role.environment_variable,
    )


def _response(settings: ModelSettings) -> ModelSettingsResponse:
    return ModelSettingsResponse(
        tiers=[_tier(tier) for tier in settings.tiers],
        roles=[_role(role) for role in settings.roles],
        rejected_parameters=[
            RejectedParametersResponse(
                model=rejected.model,
                from_file=sorted(rejected.from_file),  # type: ignore[arg-type]
                chosen=sorted(rejected.chosen),  # type: ignore[arg-type]
                changed=_change(rejected.changed),
            )
            for rejected in settings.rejected_parameters
        ],
        unreadable=[
            UnreadableChoiceResponse(kind=choice.kind, key=choice.key)
            for choice in settings.unreadable
        ],
        bounds=BOUNDS,
    )


def _settings_now(
    session: DbSession, models: ModelRouter, workspace_id: uuid.UUID
) -> ModelSettingsResponse:
    return _response(model_settings(session, models, workspace_id=workspace_id))


@router.get("", operation_id="getModelSettings", response_model=ModelSettingsResponse)
def get_settings(
    session: DbSession, workspace_id: CurrentWorkspaceId, models: Models
) -> ModelSettingsResponse:
    """Every tier and role of the configuration, what it resolves to and where that comes from."""
    return _settings_now(session, models, workspace_id)


@router.put(
    "/tiers/{tier}",
    operation_id="setTierModel",
    response_model=ModelSettingsResponse,
    responses=problems(404, 409),
)
def set_tier(
    tier: SettingKey,
    body: SetTierModelRequest,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    user: CurrentUserId,
    models: Models,
) -> ModelSettingsResponse:
    """Choose the tier's model, which every role on the tier follows.

    409 while the environment sets the tier's model.
    """
    set_tier_model(
        session, models, workspace_id=workspace_id, tier=tier, model=body.model, set_by=user
    )
    return _settings_now(session, models, workspace_id)


@router.delete(
    "/tiers/{tier}",
    operation_id="removeTierModel",
    status_code=204,
    responses=problems(404),
    dependencies=[Depends(no_request_body)],
)
def remove_tier(
    tier: SettingKey, session: DbSession, workspace_id: CurrentWorkspaceId, models: Models
) -> None:
    """Go back to the configuration file's model for the tier."""
    remove_tier_model(session, models, workspace_id=workspace_id, tier=tier)


@router.put(
    "/roles/{role}",
    operation_id="setRoleChoice",
    response_model=ModelSettingsResponse,
    responses=problems(404, 409),
)
def set_role(
    role: SettingKey,
    body: RoleChoiceRequest,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    user: CurrentUserId,
    models: Models,
) -> ModelSettingsResponse:
    """Choose the role's model and parameters, in place of the choice before.

    A choice with nothing in it removes the one stored. 409 for a model while the
    environment sets the role's.
    """
    choice = RoleChoice(
        model=body.model,
        fallbacks=None if body.fallbacks is None else tuple(body.fallbacks),
        temperature=body.temperature,
        max_tokens=body.max_tokens,
        reasoning_effort=body.reasoning_effort,
    )
    set_role_choice(
        session, models, workspace_id=workspace_id, role=role, choice=choice, set_by=user
    )
    return _settings_now(session, models, workspace_id)


@router.delete(
    "/roles/{role}",
    operation_id="removeRoleChoice",
    status_code=204,
    responses=problems(404),
    dependencies=[Depends(no_request_body)],
)
def remove_role(
    role: SettingKey, session: DbSession, workspace_id: CurrentWorkspaceId, models: Models
) -> None:
    """Go back to the configuration file for every part of the role."""
    remove_role_choice(session, models, workspace_id=workspace_id, role=role)


@router.put(
    "/rejected-parameters",
    operation_id="setRejectedParameters",
    response_model=ModelSettingsResponse,
)
def set_rejected(
    body: SetRejectedParametersRequest,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    user: CurrentUserId,
    models: Models,
) -> ModelSettingsResponse:
    """Mark a model as rejecting parameters, which are then left out of a request to it.

    What the configuration file lists for the model stays listed.
    """
    set_rejected_parameters(
        session,
        workspace_id=workspace_id,
        model=body.model,
        parameters=body.parameters,
        set_by=user,
    )
    return _settings_now(session, models, workspace_id)


@router.get("/catalog", operation_id="getModelCatalog", response_model=ModelCatalogResponse)
def get_catalog(catalog: Catalog) -> ModelCatalogResponse:
    """The provider's models, for the picker; `available` is false when it cannot be read."""
    found = catalog.models()
    if found is None:
        return ModelCatalogResponse(available=False, models=[])
    return ModelCatalogResponse(
        available=True,
        models=[
            CatalogModelResponse(
                id=model.id,
                name=model.name,
                context_length=model.context_length,
                prompt_price=model.prompt_price,
                completion_price=model.completion_price,
                supported_parameters=sorted(model.supported_parameters),
                input_modalities=sorted(model.input_modalities),
            )
            for model in sorted(found.values(), key=lambda model: model.id)
        ],
    )


@router.get(
    "/check",
    operation_id="checkModelSettings",
    response_model=ModelCheckResponse,
    responses=problems(409),
)
def check(
    session: DbSession, workspace_id: CurrentWorkspaceId, models: Models, catalog: Catalog
) -> ModelCheckResponse:
    """What each role's models lack for the role, according to the provider's catalogue.

    409 while a stored choice breaks a rule.
    """
    found = catalog.models()
    if found is None:
        return ModelCheckResponse(available=False, problems=[])
    by_role = model_problems(session, models, found, workspace_id=workspace_id)
    return ModelCheckResponse(
        available=True,
        problems=[
            ModelProblemResponse(role=role, model=problem.model, problem=problem.problem)
            for role, role_problems in by_role.items()
            for problem in role_problems
        ],
    )
