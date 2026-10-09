"""Error responses: every failure a client can act on has a status, a code and a message.

Domain and persistence errors are mapped here, in one table, so routes raise
and never build error responses themselves. Messages come from the domain
error; database failures and anything unexpected get a fixed message, because
their text may name hosts, statements or paths.

An error body carries what a client needs to act on it as a field of its own,
never inside the message: ``existing_project_id`` with ``project_already_exists``.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from openmarketer_api.identity import NotLocalRequest
from openmarketer_core.analysis_request import AnalysisNotStarted
from openmarketer_core.db.analysis_runs import AnalysisAlreadyRunning, AnalysisRunNotFound
from openmarketer_core.db.profile_versions import ProfileAlreadyApproved, ProfileVersionNotFound
from openmarketer_core.db.projects import ProjectAlreadyExists, ProjectNotFound
from openmarketer_core.db.session import DatabaseError
from openmarketer_core.intake import IntakeError

logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    INVALID_REPOSITORY_URL = "invalid_repository_url"
    NOT_LOCAL_REQUEST = "not_local_request"
    PROJECT_NOT_FOUND = "project_not_found"
    ANALYSIS_NOT_FOUND = "analysis_not_found"
    PROFILE_NOT_FOUND = "profile_not_found"
    PROJECT_ALREADY_EXISTS = "project_already_exists"
    ANALYSIS_ALREADY_RUNNING = "analysis_already_running"
    PROFILE_ALREADY_APPROVED = "profile_already_approved"
    INTERNAL_ERROR = "internal_error"
    ANALYSIS_NOT_STARTED = "analysis_not_started"
    DATABASE_UNAVAILABLE = "database_unavailable"


class ProfileNotFound(Exception):
    """The project has no draft, or no approved, profile to return."""


class Problem(BaseModel):
    """The body of every error response except request validation (422)."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    # Left out of the body, never null, when the error is another one.
    existing_project_id: uuid.UUID | SkipJsonSchema[None] = Field(
        default=None,
        description=(
            "Only with `project_already_exists`: the project the workspace already has"
            " for that repository."
        ),
    )


_DOMAIN_ERRORS: dict[type[Exception], tuple[int, ErrorCode]] = {
    IntakeError: (400, ErrorCode.INVALID_REPOSITORY_URL),
    NotLocalRequest: (403, ErrorCode.NOT_LOCAL_REQUEST),
    ProjectNotFound: (404, ErrorCode.PROJECT_NOT_FOUND),
    AnalysisRunNotFound: (404, ErrorCode.ANALYSIS_NOT_FOUND),
    ProfileVersionNotFound: (404, ErrorCode.PROFILE_NOT_FOUND),
    ProfileNotFound: (404, ErrorCode.PROFILE_NOT_FOUND),
    AnalysisAlreadyRunning: (409, ErrorCode.ANALYSIS_ALREADY_RUNNING),
    ProfileAlreadyApproved: (409, ErrorCode.PROFILE_ALREADY_APPROVED),
    AnalysisNotStarted: (503, ErrorCode.ANALYSIS_NOT_STARTED),
}


def problems(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """The error responses a route declares in the OpenAPI document."""
    return {status: {"model": Problem} for status in statuses}


def register_error_handlers(app: FastAPI) -> None:
    for error_type, (status, code) in _DOMAIN_ERRORS.items():
        app.add_exception_handler(error_type, _domain_error_handler(status, code))
    app.add_exception_handler(ProjectAlreadyExists, _project_already_exists)
    app.add_exception_handler(DatabaseError, _database_error)
    app.add_exception_handler(Exception, _unexpected_error)


def _problem(
    status: int, code: ErrorCode, message: str, *, existing_project_id: uuid.UUID | None = None
) -> JSONResponse:
    problem = Problem(code=code, message=message, existing_project_id=existing_project_id)
    return JSONResponse(problem.model_dump(mode="json", exclude_none=True), status)


def _domain_error_handler(
    status: int, code: ErrorCode
) -> Callable[[Request, Exception], JSONResponse]:
    def handler(_: Request, error: Exception) -> JSONResponse:
        return _problem(status, code, str(error))

    return handler


def _project_already_exists(_: Request, error: Exception) -> JSONResponse:
    existing = error.project_id if isinstance(error, ProjectAlreadyExists) else None
    return _problem(409, ErrorCode.PROJECT_ALREADY_EXISTS, str(error), existing_project_id=existing)


def _database_error(_: Request, error: Exception) -> JSONResponse:
    logger.error("database: %s", error)
    return _problem(
        503, ErrorCode.DATABASE_UNAVAILABLE, "the database could not complete the request"
    )


def _unexpected_error(_: Request, __: Exception) -> JSONResponse:
    # The server logs the traceback; the client gets no detail of it.
    return _problem(500, ErrorCode.INTERNAL_ERROR, "the request failed unexpectedly")
