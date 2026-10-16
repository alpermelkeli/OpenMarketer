"""Error responses: every failure a client can act on has a status, a code and a message.

Domain and persistence errors are mapped here, in one table, so routes raise
and never build error responses themselves. Messages come from the domain
error; database failures and anything unexpected get a fixed message, because
their text may name hosts, statements or paths.

A credential value, a choice of models or a limit of an analysis that breaks a
rule is answered in the shape of a request validation error (422), beside the
field it was sent in: it is a refusal of what the body holds, like a field of
the wrong type.

A chat turn the agent gave no answer for is answered with a fixed message: the
agent's own wording of a failed turn may repeat what the model sent.

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
from openmarketer_core.credentials.catalog import CredentialValueRefused, UnknownCredential
from openmarketer_core.credentials.resolution import (
    CredentialSetByEnvironment,
    CredentialUnreadable,
    EncryptionKeyMissing,
    ModelKeyNotSet,
)
from openmarketer_core.db.analysis_runs import AnalysisAlreadyRunning, AnalysisRunNotFound
from openmarketer_core.db.chat import ChatNotFound
from openmarketer_core.db.profile_versions import ProfileAlreadyApproved, ProfileVersionNotFound
from openmarketer_core.db.project_removal import (
    ProjectHasPendingProposal,
    ProjectHasUnfinishedRun,
)
from openmarketer_core.db.projects import ProjectAlreadyExists, ProjectNotFound
from openmarketer_core.db.proposals import ProposalNotFound, ProposalNotPending
from openmarketer_core.db.session import DatabaseError
from openmarketer_core.llm import LLMError
from openmarketer_core.orchestrator.chat_agent.rules import ChatTurnError
from openmarketer_core.repository_analysis.intake import IntakeError
from openmarketer_core.repository_analysis.request import AnalysisNotStarted
from openmarketer_core.workspace_settings.analysis_limits import AnalysisLimitsRefused
from openmarketer_core.workspace_settings.model_choices import ModelChoiceRefused
from openmarketer_core.workspace_settings.model_settings import (
    ModelSetByEnvironment,
    StoredModelChoiceInvalid,
    UnknownRole,
    UnknownTier,
)

logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    INVALID_REPOSITORY_URL = "invalid_repository_url"
    NOT_LOCAL_REQUEST = "not_local_request"
    PROJECT_NOT_FOUND = "project_not_found"
    ANALYSIS_NOT_FOUND = "analysis_not_found"
    CHAT_NOT_FOUND = "chat_not_found"
    PROFILE_NOT_FOUND = "profile_not_found"
    CREDENTIAL_NOT_FOUND = "credential_not_found"
    PROPOSAL_NOT_FOUND = "proposal_not_found"
    TIER_NOT_FOUND = "tier_not_found"
    ROLE_NOT_FOUND = "role_not_found"
    PROJECT_ALREADY_EXISTS = "project_already_exists"
    ANALYSIS_ALREADY_RUNNING = "analysis_already_running"
    PROFILE_ALREADY_APPROVED = "profile_already_approved"
    PROPOSAL_NOT_PENDING = "proposal_not_pending"
    PROPOSAL_PENDING = "proposal_pending"
    CREDENTIAL_SET_BY_ENVIRONMENT = "credential_set_by_environment"
    CREDENTIAL_UNREADABLE = "credential_unreadable"
    ENCRYPTION_KEY_MISSING = "encryption_key_missing"
    MODEL_SET_BY_ENVIRONMENT = "model_set_by_environment"
    MODEL_KEY_NOT_SET = "model_key_not_set"
    STORED_SETTING_INVALID = "stored_setting_invalid"
    INTERNAL_ERROR = "internal_error"
    ANALYSIS_NOT_STARTED = "analysis_not_started"
    MODEL_UNAVAILABLE = "model_unavailable"
    CHAT_UNANSWERED = "chat_unanswered"
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
    ChatNotFound: (404, ErrorCode.CHAT_NOT_FOUND),
    ProfileVersionNotFound: (404, ErrorCode.PROFILE_NOT_FOUND),
    ProfileNotFound: (404, ErrorCode.PROFILE_NOT_FOUND),
    UnknownCredential: (404, ErrorCode.CREDENTIAL_NOT_FOUND),
    ProposalNotFound: (404, ErrorCode.PROPOSAL_NOT_FOUND),
    UnknownTier: (404, ErrorCode.TIER_NOT_FOUND),
    UnknownRole: (404, ErrorCode.ROLE_NOT_FOUND),
    AnalysisAlreadyRunning: (409, ErrorCode.ANALYSIS_ALREADY_RUNNING),
    ProjectHasUnfinishedRun: (409, ErrorCode.ANALYSIS_ALREADY_RUNNING),
    ProfileAlreadyApproved: (409, ErrorCode.PROFILE_ALREADY_APPROVED),
    ProposalNotPending: (409, ErrorCode.PROPOSAL_NOT_PENDING),
    ProjectHasPendingProposal: (409, ErrorCode.PROPOSAL_PENDING),
    CredentialSetByEnvironment: (409, ErrorCode.CREDENTIAL_SET_BY_ENVIRONMENT),
    CredentialUnreadable: (409, ErrorCode.CREDENTIAL_UNREADABLE),
    EncryptionKeyMissing: (409, ErrorCode.ENCRYPTION_KEY_MISSING),
    ModelSetByEnvironment: (409, ErrorCode.MODEL_SET_BY_ENVIRONMENT),
    ModelKeyNotSet: (409, ErrorCode.MODEL_KEY_NOT_SET),
    StoredModelChoiceInvalid: (409, ErrorCode.STORED_SETTING_INVALID),
    AnalysisNotStarted: (503, ErrorCode.ANALYSIS_NOT_STARTED),
    LLMError: (503, ErrorCode.MODEL_UNAVAILABLE),
}


def problems(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """The error responses a route declares in the OpenAPI document."""
    return {status: {"model": Problem} for status in statuses}


def register_error_handlers(app: FastAPI) -> None:
    for error_type, (status, code) in _DOMAIN_ERRORS.items():
        app.add_exception_handler(error_type, _domain_error_handler(status, code))
    app.add_exception_handler(ProjectAlreadyExists, _project_already_exists)
    app.add_exception_handler(CredentialValueRefused, _credential_value_refused)
    app.add_exception_handler(ModelChoiceRefused, _setting_refused)
    app.add_exception_handler(AnalysisLimitsRefused, _setting_refused)
    app.add_exception_handler(ChatTurnError, _chat_unanswered)
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


def _credential_value_refused(_: Request, error: Exception) -> JSONResponse:
    # The message names the rule that was broken and never the value.
    refusal = {"type": "value_error", "loc": ["body", "value"], "msg": str(error)}
    return JSONResponse({"detail": [refusal]}, 422)


def _setting_refused(_: Request, error: Exception) -> JSONResponse:
    # The message names the rule that was broken; the field is the part of the body that did.
    field = getattr(error, "field", "")
    refusal = {"type": "value_error", "loc": ["body", field], "msg": str(error)}
    return JSONResponse({"detail": [refusal]}, 422)


def _chat_unanswered(_: Request, __: Exception) -> JSONResponse:
    # The agent's own wording of a failed turn may repeat what the model sent.
    return _problem(
        503,
        ErrorCode.CHAT_UNANSWERED,
        "the chat agent gave no answer; sending the message again may answer it",
    )


def _database_error(_: Request, error: Exception) -> JSONResponse:
    logger.error("database: %s", error)
    return _problem(
        503, ErrorCode.DATABASE_UNAVAILABLE, "the database could not complete the request"
    )


def _unexpected_error(_: Request, __: Exception) -> JSONResponse:
    # The server logs the traceback; the client gets no detail of it.
    return _problem(500, ErrorCode.INTERNAL_ERROR, "the request failed unexpectedly")
