"""The caps a workspace's spending on generated media runs under, and changing them.

Each cap is resolved on its own, in the order of every other setting: an
environment variable wins (``MEDIA_MONTHLY_CAP_USD``, ``MEDIA_AUTO_APPROVE_USD``),
then what the workspace chose in the dashboard (``creative_budget.py``), then the
default. A variable that is empty or only whitespace is not set. A value the
environment gives is held to the same bounds as a choice, and one that breaks
them is ``CreativeBudgetEnvironmentInvalid``, which names the variable. Choosing a
cap the environment sets is refused (``CreativeBudgetSetByEnvironment``), since it
would have no effect; removing a choice is always allowed.

The choice is stored under ``creative.budget`` and checked again when it is read:
one that breaks a rule is ``StoredCreativeBudgetInvalid`` for a run, which says
what to reset, and is marked unreadable by ``creative_budget_setting``, so the
screen that resets it still opens.

Every function receives the session, the environment as a mapping and the
workspace. None reads the process environment or commits, and none holds money:
that is ``budget/ledger.py``, which the caps resolved here are given to.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy.orm import Session

from openmarketer_core.budget.caps import CreativeCaps
from openmarketer_core.db.workspace_settings import remove_setting, store_setting, stored_setting
from openmarketer_core.workspace_settings.creative_budget import (
    DEFAULT_CAPS,
    CreativeBudgetChoice,
    CreativeBudgetRefused,
    acceptable_amount,
    acceptable_budget,
    budget_from_stored,
)

CREATIVE_BUDGET_SETTING = "creative.budget"
MONTHLY_CAP_VARIABLE = "MEDIA_MONTHLY_CAP_USD"
AUTO_APPROVE_VARIABLE = "MEDIA_AUTO_APPROVE_USD"

# Each cap and the environment variable that sets it.
_VARIABLES = {"monthly_usd": MONTHLY_CAP_VARIABLE, "auto_per_action_usd": AUTO_APPROVE_VARIABLE}


class StoredCreativeBudgetInvalid(Exception):
    """The stored caps break a rule; the message says what to reset."""

    def __init__(self) -> None:
        super().__init__(
            "the stored budget for generated media is not valid any more: reset it in the"
            " dashboard's Settings"
        )


class CreativeBudgetEnvironmentInvalid(Exception):
    """An environment variable sets a cap to a value that breaks its rule."""

    def __init__(self, environment_variable: str, rule: str) -> None:
        super().__init__(f"the environment variable {environment_variable} is not valid: {rule}")
        self.environment_variable = environment_variable


class CreativeBudgetSetByEnvironment(Exception):
    """The environment sets the cap, so a stored choice of it would never be used."""

    def __init__(self, environment_variable: str) -> None:
        super().__init__(
            f"the environment variable {environment_variable} sets this cap; unset it to"
            " choose it here"
        )
        self.environment_variable = environment_variable


class CapSource(StrEnum):
    """Where the value of a cap in use comes from."""

    ENVIRONMENT = "environment"
    STORED = "stored"
    DEFAULT = "default"


@dataclass(frozen=True)
class CapSetting:
    """One cap: its value in use, where that comes from, its default and its variable."""

    value: Decimal
    source: CapSource
    default: Decimal
    environment_variable: str


@dataclass(frozen=True)
class CreativeBudgetSetting:
    """The caps in use, each with its source, and what the workspace chose, if anything.

    ``unreadable`` is a stored choice that breaks a rule: it is left out, and spending is
    refused until it is reset.
    """

    caps: CreativeCaps
    monthly: CapSetting
    auto_per_action: CapSetting
    choice: CreativeBudgetChoice | None
    set_at: datetime | None
    set_by: uuid.UUID | None
    unreadable: bool = False


def creative_caps(
    session: Session, environ: Mapping[str, str], *, workspace_id: uuid.UUID
) -> CreativeCaps:
    """The caps the workspace's spending on generated media runs under now.

    Raises ``CreativeBudgetEnvironmentInvalid`` and ``StoredCreativeBudgetInvalid``.
    """
    setting = creative_budget_setting(session, environ, workspace_id=workspace_id)
    if setting.unreadable:
        raise StoredCreativeBudgetInvalid
    return setting.caps


def creative_budget_setting(
    session: Session, environ: Mapping[str, str], *, workspace_id: uuid.UUID
) -> CreativeBudgetSetting:
    """The workspace's caps and where each comes from.

    Raises ``CreativeBudgetEnvironmentInvalid``.
    """
    stored = stored_setting(session, workspace_id=workspace_id, name=CREATIVE_BUDGET_SETTING)
    choice: CreativeBudgetChoice | None = None
    unreadable = False
    if stored is not None:
        try:
            choice = budget_from_stored(stored.value)
        except CreativeBudgetRefused:
            unreadable = True
    monthly = _cap("monthly_usd", environ, choice, DEFAULT_CAPS.monthly_usd)
    auto = _cap("auto_per_action_usd", environ, choice, DEFAULT_CAPS.auto_per_action_usd)
    return CreativeBudgetSetting(
        caps=CreativeCaps(monthly_usd=monthly.value, auto_per_action_usd=auto.value),
        monthly=monthly,
        auto_per_action=auto,
        choice=choice,
        set_at=None if stored is None else stored.set_at,
        set_by=None if stored is None else stored.set_by,
        unreadable=unreadable,
    )


def set_creative_budget(
    session: Session,
    environ: Mapping[str, str],
    *,
    workspace_id: uuid.UUID,
    choice: CreativeBudgetChoice,
    set_by: uuid.UUID,
) -> CreativeBudgetSetting:
    """Store ``choice`` in place of the one there; a choice with nothing in it removes it.

    Raises ``CreativeBudgetRefused``, ``CreativeBudgetSetByEnvironment`` for a cap the
    environment sets, and ``CreativeBudgetEnvironmentInvalid``.
    """
    accepted = acceptable_budget(choice)
    for field, value in (
        ("monthly_usd", accepted.monthly_usd),
        ("auto_per_action_usd", accepted.auto_per_action_usd),
    ):
        if value is not None and _from_environment(field, environ) is not None:
            raise CreativeBudgetSetByEnvironment(_VARIABLES[field])
    if accepted.is_empty:
        return remove_creative_budget(session, environ, workspace_id=workspace_id)
    store_setting(
        session,
        workspace_id=workspace_id,
        name=CREATIVE_BUDGET_SETTING,
        value=accepted.as_stored(),
        set_by=set_by,
    )
    return creative_budget_setting(session, environ, workspace_id=workspace_id)


def remove_creative_budget(
    session: Session, environ: Mapping[str, str], *, workspace_id: uuid.UUID
) -> CreativeBudgetSetting:
    """Go back to the environment's caps and the defaults."""
    remove_setting(session, workspace_id=workspace_id, name=CREATIVE_BUDGET_SETTING)
    return creative_budget_setting(session, environ, workspace_id=workspace_id)


def _cap(
    field: str,
    environ: Mapping[str, str],
    choice: CreativeBudgetChoice | None,
    default: Decimal,
) -> CapSetting:
    variable = _VARIABLES[field]
    from_environment = _from_environment(field, environ)
    if from_environment is not None:
        return CapSetting(from_environment, CapSource.ENVIRONMENT, default, variable)
    chosen = None if choice is None else getattr(choice, field)
    if chosen is not None:
        return CapSetting(chosen, CapSource.STORED, default, variable)
    return CapSetting(default, CapSource.DEFAULT, default, variable)


def _from_environment(field: str, environ: Mapping[str, str]) -> Decimal | None:
    variable = _VARIABLES[field]
    text = environ.get(variable, "").strip()
    if not text:
        return None
    try:
        return acceptable_amount(field, text)
    except CreativeBudgetRefused as refused:
        raise CreativeBudgetEnvironmentInvalid(variable, str(refused)) from None
