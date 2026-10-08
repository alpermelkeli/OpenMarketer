"""The FastAPI application: routes and error handling, and the process entry point.

``create_app`` builds the application without touching the environment, which
is what the tests and the OpenAPI export use. ``app_from_env`` is what uvicorn
runs: it reads the settings and attaches the database and the analysis runner.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI

from openmarketer_api.dependencies import Services
from openmarketer_api.errors import problems, register_error_handlers
from openmarketer_api.identity import current_user_id
from openmarketer_api.in_process_runs import InProcessAnalysisRuns
from openmarketer_api.routes import analyses, health, profiles, projects
from openmarketer_api.settings import Settings
from openmarketer_core.db.session import session_factory
from openmarketer_core.extraction import discover_extractors
from openmarketer_core.llm import RouterChatModel
from openmarketer_core.repository_analysis import RepositoryAnalysis, analyze_repository

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
    for resource in (projects, analyses, profiles):
        workspace_routes.include_router(resource.router)
    app.include_router(workspace_routes)
    return app


def app_from_env() -> FastAPI:
    settings = Settings.from_env(os.environ)
    sessions = session_factory(settings.database_url)
    app = create_app()
    app.state.services = Services(
        sessions=sessions,
        analysis_runs=InProcessAnalysisRuns(sessions, _analyse_with_configured_models),
    )
    return app


def _analyse_with_configured_models(repository_url: str, clone_into: Path) -> RepositoryAnalysis:
    # No access token is passed: a token for one host must not be sent to whichever
    # host a submitted URL names, so the API analyses public repositories only.
    return analyze_repository(
        repository_url,
        clone_into,
        model=RouterChatModel.from_env(),
        extractors=discover_extractors(),
    )
