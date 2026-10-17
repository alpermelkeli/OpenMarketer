"""The FastAPI application: routes and error handling, and the process entry point.

``create_app`` builds the application without touching the environment, which
is what the tests and the OpenAPI export use. ``app_from_env`` is what uvicorn
runs: it reads the settings and attaches the database, the workflow starter,
the environment, the cipher credentials are stored with, the model
configuration, the provider's catalogue of models, the object storage the pages
of deleted research runs are removed from, the object storage the studio's
images are read from and removed from, and whether its turns are traced to
Langfuse. Neither connects to anything: the database, the workflow server and
the object storage are reached when a request first needs them.

Tracing is started here when the environment gives Langfuse's keys, the way the
worker starts it, and every chat turn runs under its handler
(``routes/chat.py``). It is not stopped by hand: the handler was set in the
factory's context, which the server's own tasks do not share, so ``Tracing.stop``
could not unset it; Langfuse's own ``atexit`` sends what is still queued when the
process ends.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, FastAPI

from openmarketer_api.dependencies import Services
from openmarketer_api.errors import problems, register_error_handlers
from openmarketer_api.identity import current_user_id
from openmarketer_api.routes import (
    analyses,
    analysis_settings,
    chat,
    creative_budget,
    credentials,
    health,
    model_settings,
    profiles,
    projects,
    proposals,
    research,
    studio,
)
from openmarketer_api.settings import Settings
from openmarketer_api.temporal_workflows import TemporalWorkflows
from openmarketer_core.db.session import session_factory
from openmarketer_core.llm_catalog import ModelCatalog
from openmarketer_core.research.page_store import S3PageStore
from openmarketer_core.studio.asset_store import S3AssetStore
from openmarketer_core.tracing import start_tracing

logger = logging.getLogger(__name__)

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
    for resource in (
        projects,
        analyses,
        research,
        studio,
        profiles,
        proposals,
        chat,
        credentials,
        model_settings,
        analysis_settings,
        creative_budget,
    ):
        workspace_routes.include_router(resource.router)
    app.include_router(workspace_routes)
    return app


def app_from_env() -> FastAPI:
    settings = Settings.from_env(os.environ)
    tracing = start_tracing(settings.tracing)
    logger.info("%s", settings.tracing.describe())
    app = create_app()
    app.state.services = Services(
        sessions=session_factory(settings.database_url),
        workflows=TemporalWorkflows(settings.workflow_server),
        environment=os.environ,
        credential_cipher=settings.credential_cipher,
        model_router=settings.model_router,
        model_catalog=ModelCatalog(settings.model_router.config.provider.base_url),
        page_store=S3PageStore.from_env(os.environ),
        asset_store=S3AssetStore.from_env(os.environ),
        tracing=tracing,
    )
    return app
