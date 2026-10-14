"""The limits an analysis of a workspace runs under, and changing them.

What the workspace chose (``analysis_limits.py``) is laid over the analyzer's
own defaults; a limit it did not choose keeps the default. There is no
environment variable for either limit, so nothing overrides a stored choice.

The choice is stored under ``analysis.limits`` and checked again when it is
read: one that breaks a rule is ``StoredAnalysisLimitsInvalid`` for a run, which
says what to reset, and is marked unreadable by ``analysis_limits_setting``, so
the screen that resets it still opens. Every function receives the session and
the workspace; none commits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from openmarketer_core.db.workspace_settings import remove_setting, store_setting, stored_setting
from openmarketer_core.repository_analysis.analyzer_agent.rules import Limits
from openmarketer_core.workspace_settings.analysis_limits import (
    AnalysisLimitsChoice,
    AnalysisLimitsRefused,
    acceptable_limits,
    limits_from_stored,
)

ANALYSIS_LIMITS_SETTING = "analysis.limits"


class StoredAnalysisLimitsInvalid(Exception):
    """The stored limits break a rule; the message says what to reset."""

    def __init__(self) -> None:
        super().__init__(
            "the stored limits of an analysis are not valid any more: reset them in the"
            " dashboard's Settings, Analysis"
        )


@dataclass(frozen=True)
class AnalysisLimitsSetting:
    """The limits in use, the analyzer's defaults, and what the workspace chose, if anything.

    ``unreadable`` is a stored choice that breaks a rule: the defaults are shown in its
    place, and a run is refused until it is reset.
    """

    limits: Limits
    defaults: Limits
    choice: AnalysisLimitsChoice | None
    set_at: datetime | None
    set_by: uuid.UUID | None
    unreadable: bool = False


def analysis_limits(session: Session, *, workspace_id: uuid.UUID) -> Limits:
    """The limits an analysis of the workspace runs under now.

    Raises ``StoredAnalysisLimitsInvalid``.
    """
    setting = analysis_limits_setting(session, workspace_id=workspace_id)
    if setting.unreadable:
        raise StoredAnalysisLimitsInvalid
    return setting.limits


def analysis_limits_setting(session: Session, *, workspace_id: uuid.UUID) -> AnalysisLimitsSetting:
    """The limits of the workspace's analyses and where they come from."""
    defaults = Limits()
    stored = stored_setting(session, workspace_id=workspace_id, name=ANALYSIS_LIMITS_SETTING)
    if stored is None:
        return AnalysisLimitsSetting(defaults, defaults, None, None, None)
    try:
        choice = limits_from_stored(stored.value)
    except AnalysisLimitsRefused:
        return AnalysisLimitsSetting(
            defaults, defaults, None, stored.set_at, stored.set_by, unreadable=True
        )
    return AnalysisLimitsSetting(
        limits=choice.applied_to(defaults),
        defaults=defaults,
        choice=choice,
        set_at=stored.set_at,
        set_by=stored.set_by,
    )


def set_analysis_limits(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    choice: AnalysisLimitsChoice,
    set_by: uuid.UUID,
) -> AnalysisLimitsSetting:
    """Store ``choice`` in place of the one there; a choice with nothing in it removes it.

    Raises ``AnalysisLimitsRefused``.
    """
    accepted = acceptable_limits(choice)
    if accepted.is_empty:
        return remove_analysis_limits(session, workspace_id=workspace_id)
    store_setting(
        session,
        workspace_id=workspace_id,
        name=ANALYSIS_LIMITS_SETTING,
        value=accepted.as_stored(),
        set_by=set_by,
    )
    return analysis_limits_setting(session, workspace_id=workspace_id)


def remove_analysis_limits(session: Session, *, workspace_id: uuid.UUID) -> AnalysisLimitsSetting:
    """Go back to the analyzer's defaults."""
    remove_setting(session, workspace_id=workspace_id, name=ANALYSIS_LIMITS_SETTING)
    return analysis_limits_setting(session, workspace_id=workspace_id)
