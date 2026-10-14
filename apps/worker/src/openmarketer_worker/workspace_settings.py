"""The settings a workspace chose in the dashboard, as an attempt of any agent gets them.

A workspace can choose its models (a tier's, a role's, the parameters a model
rejects) and the limits of an analysis from the dashboard, after the worker
started too. So none is read when the worker starts: ``WorkspaceSettings``
reads them when an attempt asks, inside the activity that uses them, and keeps
none. A choice made while a run is in progress is taken by the run's next
attempt.

Which choice wins over the configuration file and the environment is core's
rule (``workspace_settings/``); the environment still wins wherever it sets the
same thing. A stored choice that breaks a rule is core's
``StoredModelChoiceInvalid`` or ``StoredAnalysisLimitsInvalid``, whose message
says what to reset and may be shown. A failure of the database is not caught.
Nothing here reads the process's environment. It is for activities: the
workflow sandbox does not load it.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.session import transaction
from openmarketer_core.llm_config import ModelRouter
from openmarketer_core.repository_analysis.analyzer_agent import Limits
from openmarketer_core.workspace_settings.analysis_settings import analysis_limits
from openmarketer_core.workspace_settings.model_settings import model_choices


class WorkspaceSettings:
    """A workspace's choices from the dashboard, read each time they are asked for."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def router_for(self, workspace_id: uuid.UUID, router: ModelRouter) -> ModelRouter:
        """``router`` with the workspace's model choices laid over it.

        Raises ``StoredModelChoiceInvalid`` and ``DatabaseError``.
        """
        with transaction(self._sessions) as session:
            choices = model_choices(session, router, workspace_id=workspace_id)
        return choices.router_for(router)

    def analysis_limits(self, workspace_id: uuid.UUID) -> Limits:
        """The limits an analysis of the workspace runs under.

        Raises ``StoredAnalysisLimitsInvalid`` and ``DatabaseError``.
        """
        with transaction(self._sessions) as session:
            return analysis_limits(session, workspace_id=workspace_id)
