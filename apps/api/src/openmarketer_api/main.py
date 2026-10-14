"""The FastAPI application: routes and error handling, and the process entry point.

``create_app`` builds the application without touching the environment, which
is what the tests and the OpenAPI export use. ``app_from_env`` is what uvicorn
runs: it reads the settings and attaches the database, the workflow starter,
the signals that tell a run of a decision, the environment and the cipher
credentials are stored with.
Neither connects to anything: the database and the workflow server are reached
when a request first needs them.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, FastAPI

from openmarketer_api.dependencies import Services
from openmarketer_api.errors import problems, register_error_handlers
from openmarketer_api.identity import current_user_id
from openmarketer_api.routes import analyses, credentials, health, profiles, projects, proposals
from openmarketer_api.settings import Settings
from openmarketer_api.temporal_workflows import (
    TemporalAnalysisWorkflows,
    TemporalConnection,
    TemporalRunSignals,
)
from openmarketer_core.db.session import session_factory
from openmarketer_core.repository_analysis.workflow_contract import ANALYSIS_TASK_QUEUE

API_VERSION = "0.1.0"


def create_app() -> FastAPI:
    # One ProductProfile schema for requests and responses keeps the generated types simple.
    app = FastAPI(
        title="OpenMarketer API", version=API_VERSION, separate_input_output_schemas=False
    )
    register_error_handlers(app)
    app.include_router(health.router)

    # Every route below needs a user, and so does every route added to it later.
    workspace_routes = APIRouter(
        prefix="/v1", dependencies=[Depends(current_user_id)], responses=problems(403, 503)
    )
    for resource in (projects, analyses, profiles, proposals, credentials):
        workspace_routes.include_router(resource.router)
    app.include_router(workspace_routes)
    return app


def app_from_env() -> FastAPI:
    settings = Settings.from_env(os.environ)
    app = create_app()
    temporal = TemporalConnection(settings.workflow_server)
    app.state.services = Services(
        sessions=session_factory(settings.database_url),
        analysis_workflows=TemporalAnalysisWorkflows(temporal, task_queue=ANALYSIS_TASK_QUEUE),
        run_signals=TemporalRunSignals(temporal),
        environment=os.environ,
        credential_cipher=settings.credential_cipher,
    )
    return app
