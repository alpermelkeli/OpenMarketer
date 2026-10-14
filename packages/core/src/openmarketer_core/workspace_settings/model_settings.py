"""Which models a workspace's roles use, where each choice comes from, and changing a choice.

The router's order holds (``llm_config.py``): an environment variable wins,
then what the workspace chose, then the configuration file. A choice the
environment would override is refused, since it would have no effect: the
model of a tier while ``LLM_MODEL_<TIER>`` is set, and a role's model while
``LLM_MODEL__<ROLE>`` is set (the role's other parts are still taken).
Removing a choice is always allowed.

A choice is stored under a name of its own (``model.tier.<tier>``,
``model.role.<role>``, ``model.rejected_parameters.<model>``) and checked again
when it is read: a stored choice that breaks a rule is
``StoredModelChoiceInvalid`` for a run, which says what to reset, and is listed
as unreadable by ``model_settings``, so the screen that resets it still opens;
one for a tier or a role the configuration no longer has is ignored.

Every function receives what it needs: the session, the router built from the
configuration file and the environment, and the workspace. None reads the
process environment or a file, fetches the catalogue or commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy.orm import Session

from openmarketer_core.db.workspace_settings import (
    StoredSetting,
    remove_setting,
    store_setting,
    stored_settings,
)
from openmarketer_core.llm_config import (
    CatalogModel,
    ConfigError,
    ModelProblem,
    ModelRouter,
    Resolved,
)
from openmarketer_core.workspace_settings.model_choices import (
    ModelChoiceRefused,
    RoleChoice,
    WorkspaceModelChoices,
    acceptable_model,
    acceptable_rejected_parameters,
    acceptable_role_choice,
    role_choice_from_stored,
)

MODEL_SETTINGS = "model."
TIER_SETTING = "model.tier."
ROLE_SETTING = "model.role."
REJECTED_PARAMETERS_SETTING = "model.rejected_parameters."


class UnknownTier(Exception):
    """The configuration has no tier of that name."""

    def __init__(self) -> None:
        super().__init__("there is no such tier")


class UnknownRole(Exception):
    """The configuration has no role of that name."""

    def __init__(self) -> None:
        super().__init__("there is no such role")


class ModelSetByEnvironment(Exception):
    """The environment sets the model, so a stored choice of it would never be used."""

    def __init__(self, environment_variable: str) -> None:
        super().__init__(
            f"the environment variable {environment_variable} sets this model; unset it to"
            " choose one here instead"
        )
        self.environment_variable = environment_variable


class StoredModelChoiceInvalid(Exception):
    """A stored choice breaks a rule; the message says what to reset."""

    def __init__(self, what: str) -> None:
        super().__init__(
            f"the stored choice for {what} is not valid any more: reset it in the dashboard's"
            " Settings, Models"
        )


class TierModelSource(StrEnum):
    """Where the model of a tier comes from."""

    ENVIRONMENT = "environment"
    WORKSPACE = "workspace"
    FILE = "file"


class RoleModelSource(StrEnum):
    """Where the model of a role comes from; with ``TIER``, its tier says the rest."""

    ENVIRONMENT = "environment"
    WORKSPACE = "workspace"
    FILE = "file"
    TIER = "tier"
    DEFAULT = "default"


@dataclass(frozen=True)
class SettingChange:
    """Who stored a choice and when."""

    set_at: datetime
    set_by: uuid.UUID


@dataclass(frozen=True)
class TierSetting:
    tier: str
    model: str
    source: TierModelSource
    file_model: str
    environment_variable: str
    chosen: str | None
    changed: SettingChange | None


@dataclass(frozen=True)
class RoleSetting:
    """One role as it resolves now, as it would without the workspace's choice, and that choice.

    ``inherited`` keeps the workspace's tiers: it is what removing the role's own
    choice leads to.
    """

    role: str
    kind: str
    tier: str | None
    requires: tuple[str, ...]
    effective: Resolved
    model_source: RoleModelSource
    temperature_sent: bool
    inherited: Resolved
    choice: RoleChoice | None
    changed: SettingChange | None
    environment_variable: str


@dataclass(frozen=True)
class RejectedParametersSetting:
    """The parameters left out of a request to ``model``: the file's and the workspace's."""

    model: str
    from_file: frozenset[str]
    chosen: frozenset[str]
    changed: SettingChange | None


class ChoiceKind(StrEnum):
    TIER = "tier"
    ROLE = "role"
    REJECTED_PARAMETERS = "rejected_parameters"


@dataclass(frozen=True)
class UnreadableChoice:
    """A stored choice that breaks a rule: the tier, role or model it is for."""

    kind: ChoiceKind
    key: str


@dataclass(frozen=True)
class ModelSettings:
    """``unreadable`` lists stored choices that break a rule; they are left out of the rest."""

    tiers: tuple[TierSetting, ...]
    roles: tuple[RoleSetting, ...]
    rejected_parameters: tuple[RejectedParametersSetting, ...]
    unreadable: tuple[UnreadableChoice, ...]


@dataclass(frozen=True)
class _Stored:
    """The workspace's readable model choices, when each was stored, and those it cannot read."""

    choices: WorkspaceModelChoices
    changes: Mapping[str, SettingChange]
    unreadable: tuple[UnreadableChoice, ...]


def model_choices(
    session: Session, router: ModelRouter, *, workspace_id: uuid.UUID
) -> WorkspaceModelChoices:
    """What the workspace chose about its models. Raises ``StoredModelChoiceInvalid``."""
    stored = _stored(session, router, workspace_id)
    if stored.unreadable:
        raise StoredModelChoiceInvalid(_what(stored.unreadable[0]))
    return stored.choices


def model_settings(
    session: Session, router: ModelRouter, *, workspace_id: uuid.UUID
) -> ModelSettings:
    """Every tier and role of the configuration in the workspace, in the file's order."""
    stored = _stored(session, router, workspace_id)
    chosen = stored.choices.router_for(router)
    config = router.config
    tiers = tuple(
        _tier_setting(chosen, tier, file_model, stored) for tier, file_model in config.tiers.items()
    )
    roles = tuple(_role_setting(router, chosen, role, stored) for role in config.roles)
    models = sorted(set(config.unsupported_parameters) | set(stored.choices.rejected_parameters))
    rejected = tuple(
        RejectedParametersSetting(
            model=model,
            from_file=frozenset(config.unsupported_parameters.get(model, [])),
            chosen=stored.choices.rejected_parameters.get(model, frozenset()),
            changed=stored.changes.get(REJECTED_PARAMETERS_SETTING + model),
        )
        for model in models
    )
    return ModelSettings(
        tiers=tiers, roles=roles, rejected_parameters=rejected, unreadable=stored.unreadable
    )


def model_problems(
    session: Session,
    router: ModelRouter,
    catalog: Mapping[str, CatalogModel],
    *,
    workspace_id: uuid.UUID,
) -> dict[str, list[ModelProblem]]:
    """What each role's models lack for the role, by role; a role without a problem is left out."""
    chosen = model_choices(session, router, workspace_id=workspace_id).router_for(router)
    problems = {
        role: chosen.check_capabilities(chosen.resolve(role), catalog)
        for role in router.config.roles
    }
    return {role: found for role, found in problems.items() if found}


def set_tier_model(
    session: Session,
    router: ModelRouter,
    *,
    workspace_id: uuid.UUID,
    tier: str,
    model: str,
    set_by: uuid.UUID,
) -> None:
    """Store ``model`` as the workspace's model for ``tier``, in place of the one there.

    Raises ``UnknownTier``, ``ModelSetByEnvironment`` and ``ModelChoiceRefused``.
    """
    if tier not in router.config.tiers:
        raise UnknownTier
    variable = _tier_variable(tier)
    if router.env.get(variable, "").strip():
        raise ModelSetByEnvironment(variable)
    accepted = acceptable_model(model, "model")
    store_setting(
        session,
        workspace_id=workspace_id,
        name=TIER_SETTING + tier,
        value={"model": accepted},
        set_by=set_by,
    )


def remove_tier_model(
    session: Session, router: ModelRouter, *, workspace_id: uuid.UUID, tier: str
) -> None:
    """Remove the workspace's model for ``tier``, if it has one. Raises ``UnknownTier``."""
    if tier not in router.config.tiers:
        raise UnknownTier
    remove_setting(session, workspace_id=workspace_id, name=TIER_SETTING + tier)


def set_role_choice(
    session: Session,
    router: ModelRouter,
    *,
    workspace_id: uuid.UUID,
    role: str,
    choice: RoleChoice,
    set_by: uuid.UUID,
) -> None:
    """Store ``choice`` as the workspace's choice for ``role``, in place of the one there.

    A choice with nothing in it removes the stored one. Raises ``UnknownRole``,
    ``ModelSetByEnvironment`` for a model while the environment sets the role's, and
    ``ModelChoiceRefused``.
    """
    role_config = router.config.roles.get(role)
    if role_config is None:
        raise UnknownRole
    variable = _role_variable(role)
    if choice.model is not None and router.env.get(variable, "").strip():
        raise ModelSetByEnvironment(variable)
    accepted = acceptable_role_choice(choice, role_config)
    name = ROLE_SETTING + role
    if accepted.is_empty:
        remove_setting(session, workspace_id=workspace_id, name=name)
        return
    store_setting(
        session,
        workspace_id=workspace_id,
        name=name,
        value=accepted.as_override(),
        set_by=set_by,
    )


def remove_role_choice(
    session: Session, router: ModelRouter, *, workspace_id: uuid.UUID, role: str
) -> None:
    """Remove the workspace's choice for ``role``, if it has one. Raises ``UnknownRole``."""
    if role not in router.config.roles:
        raise UnknownRole
    remove_setting(session, workspace_id=workspace_id, name=ROLE_SETTING + role)


def set_rejected_parameters(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    model: str,
    parameters: Collection[str],
    set_by: uuid.UUID,
) -> None:
    """Mark ``model`` as rejecting ``parameters`` in the workspace; none removes the mark.

    What the configuration file lists for the model stays listed. Raises
    ``ModelChoiceRefused``.
    """
    accepted_model = acceptable_model(model, "model")
    accepted = acceptable_rejected_parameters(parameters)
    name = REJECTED_PARAMETERS_SETTING + accepted_model
    if not accepted:
        remove_setting(session, workspace_id=workspace_id, name=name)
        return
    store_setting(
        session,
        workspace_id=workspace_id,
        name=name,
        value={"parameters": sorted(accepted)},
        set_by=set_by,
    )


def _stored(session: Session, router: ModelRouter, workspace_id: uuid.UUID) -> _Stored:
    tiers: dict[str, str] = {}
    roles: dict[str, RoleChoice] = {}
    rejected: dict[str, frozenset[str]] = {}
    changes: dict[str, SettingChange] = {}
    unreadable: list[UnreadableChoice] = []
    config = router.config
    for setting in stored_settings(session, workspace_id=workspace_id, prefix=MODEL_SETTINGS):
        name = setting.name
        if name.startswith(TIER_SETTING):
            kind, key = ChoiceKind.TIER, name.removeprefix(TIER_SETTING)
            if key not in config.tiers:
                continue
        elif name.startswith(ROLE_SETTING):
            kind, key = ChoiceKind.ROLE, name.removeprefix(ROLE_SETTING)
            if key not in config.roles:
                continue
        elif name.startswith(REJECTED_PARAMETERS_SETTING):
            kind, key = (
                ChoiceKind.REJECTED_PARAMETERS,
                name.removeprefix(REJECTED_PARAMETERS_SETTING),
            )
        else:
            continue
        try:
            if kind is ChoiceKind.TIER:
                tiers[key] = acceptable_model(setting.value.get("model"), "model")
            elif kind is ChoiceKind.ROLE:
                roles[key] = role_choice_from_stored(setting.value, config.roles[key])
            else:
                rejected[acceptable_model(key, "model")] = _rejected_from_stored(setting)
        except ModelChoiceRefused:
            unreadable.append(UnreadableChoice(kind=kind, key=key))
            continue
        changes[name] = SettingChange(set_at=setting.set_at, set_by=setting.set_by)
    choices = WorkspaceModelChoices(tiers=tiers, roles=roles, rejected_parameters=rejected)
    try:
        choices.router_for(router)
    except ConfigError:
        # Every part was checked above; a refusal here is a rule the two places disagree on.
        raise StoredModelChoiceInvalid("the models") from None
    return _Stored(choices=choices, changes=changes, unreadable=tuple(unreadable))


def _rejected_from_stored(setting: StoredSetting) -> frozenset[str]:
    parameters = setting.value.get("parameters")
    if set(setting.value) != {"parameters"} or not isinstance(parameters, list):
        raise ModelChoiceRefused("parameters", "a list of parameters")
    return acceptable_rejected_parameters(parameters)


def _tier_setting(chosen: ModelRouter, tier: str, file_model: str, stored: _Stored) -> TierSetting:
    model, source = chosen.tier_model(tier)
    return TierSetting(
        tier=tier,
        model=model,
        source=_tier_source(source),
        file_model=file_model,
        environment_variable=_tier_variable(tier),
        chosen=stored.choices.tiers.get(tier),
        changed=stored.changes.get(TIER_SETTING + tier),
    )


def _role_setting(
    router: ModelRouter, chosen: ModelRouter, role: str, stored: _Stored
) -> RoleSetting:
    config = router.config.roles[role]
    effective = chosen.resolve(role)
    inherited = stored.choices.without_role(role).router_for(router).resolve(role)
    return RoleSetting(
        role=role,
        kind=config.kind,
        tier=config.tier,
        requires=tuple(config.requires),
        effective=effective,
        model_source=_role_source(effective.source),
        temperature_sent=effective.kind == "chat"
        and "temperature" not in chosen.rejected_parameters(effective.model),
        inherited=inherited,
        choice=stored.choices.roles.get(role),
        changed=stored.changes.get(ROLE_SETTING + role),
        environment_variable=_role_variable(role),
    )


def _tier_source(source: str) -> TierModelSource:
    if source.startswith("env-tier:"):
        return TierModelSource.ENVIRONMENT
    if source.startswith("workspace-tier:"):
        return TierModelSource.WORKSPACE
    return TierModelSource.FILE


_ROLE_SOURCES = {
    "env": RoleModelSource.ENVIRONMENT,
    "workspace": RoleModelSource.WORKSPACE,
    "role": RoleModelSource.FILE,
    "default": RoleModelSource.DEFAULT,
}


def _role_source(source: str) -> RoleModelSource:
    """The router's word for where a role's model came from, as the dashboard names it.

    The router's project layer is not here: nothing stores a project's choice.
    """
    if source.partition(":")[0] in {"env-tier", "workspace-tier", "tier"}:
        return RoleModelSource.TIER
    return _ROLE_SOURCES[source]


def _tier_variable(tier: str) -> str:
    return f"LLM_MODEL_{tier.upper()}"


def _role_variable(role: str) -> str:
    return f"LLM_MODEL__{role.upper()}"


def _what(choice: UnreadableChoice) -> str:
    if choice.kind is ChoiceKind.TIER:
        return f"the {choice.key} tier"
    if choice.kind is ChoiceKind.ROLE:
        return f"the role {choice.key}"
    return "the parameters a model rejects"
