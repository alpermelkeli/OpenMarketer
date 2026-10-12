"""Credentials of the installation: where each one comes from, storing one, removing one.

Write-only. A value is taken in the body of a ``PUT`` and nothing ever returns
it or anything derived from it: a status is where the value in use comes from,
the environment variable that would set it, and who stored a value and when.
The environment wins over a stored value, so storing one for a credential the
environment sets is refused; removing a stored value is always allowed and
never touches the environment.

Who stored a value is the current user, never a field of the request. Whether
a value is one its provider accepts is not checked here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from openmarketer_api.dependencies import DbSession, Environment, StoringCipher, no_request_body
from openmarketer_api.errors import problems
from openmarketer_api.identity import CurrentUserId, CurrentWorkspaceId
from openmarketer_api.secret_bodies import SecretBodyRoute
from openmarketer_core.credentials.catalog import MAX_VALUE_LENGTH
from openmarketer_core.credentials.resolution import (
    CredentialSource,
    CredentialStatus,
    credential_statuses,
    remove_credential,
    set_credential,
)

router = APIRouter(prefix="/settings/credentials", tags=["settings"], route_class=SecretBodyRoute)

# The form every credential's name has. Anything else is a 422 that does not repeat it,
# so a secret pasted into the path by mistake is not answered back.
CredentialName = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class SetCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No length is set here: the rule is core's, and Pydantic's refusal of a long value
    # would say how long it was.
    value: SecretStr = Field(
        description=(
            "The secret. Whitespace around it is dropped; what is left is 1 to"
            f" {MAX_VALUE_LENGTH} printable ASCII characters without a space."
            " No response returns it."
        ),
    )


class StoredCredentialResponse(BaseModel):
    """That a value is stored: who stored it and when, never the value."""

    model_config = ConfigDict(extra="forbid")

    set_at: datetime
    set_by: uuid.UUID


class CredentialStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    source: CredentialSource = Field(description="Where the value in use comes from.")
    environment_variable: str = Field(
        description="The environment variable that sets this credential when it is not empty."
    )
    stored: StoredCredentialResponse | None = Field(
        description=(
            "Null when no value is stored. With the source `environment`, a stored value"
            " that the environment overrides."
        )
    )


class CredentialListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credentials: list[CredentialStatusResponse]
    can_store: bool = Field(
        description=(
            "Whether this API has an `ENCRYPTION_KEY`. Without one, storing a credential"
            " is answered 409 `encryption_key_missing`."
        )
    )


def _response(status: CredentialStatus) -> CredentialStatusResponse:
    stored = status.stored
    return CredentialStatusResponse(
        name=status.credential.name,
        source=status.source,
        environment_variable=status.credential.environment_variable,
        stored=None
        if stored is None
        else StoredCredentialResponse(set_at=stored.set_at, set_by=stored.set_by),
    )


@router.get("", operation_id="listCredentials", response_model=CredentialListResponse)
def list_all(
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    environment: Environment,
    cipher: StoringCipher,
) -> CredentialListResponse:
    """Every credential the installation has, in a fixed order, and where its value comes from."""
    statuses = credential_statuses(session, environment, workspace_id=workspace_id)
    return CredentialListResponse(
        credentials=[_response(status) for status in statuses], can_store=cipher is not None
    )


@router.put(
    "/{name}",
    operation_id="setCredential",
    response_model=CredentialStatusResponse,
    responses=problems(404, 409),
)
def set_one(
    name: CredentialName,
    body: SetCredentialRequest,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    user: CurrentUserId,
    environment: Environment,
    cipher: StoringCipher,
) -> CredentialStatusResponse:
    """Store a value for the credential, encrypted, in place of the one stored before.

    409 while the environment sets the credential, and when the API has no `ENCRYPTION_KEY`.
    """
    status = set_credential(
        session,
        environment,
        workspace_id=workspace_id,
        name=name,
        value=body.value.get_secret_value(),
        set_by=user,
        cipher=cipher,
    )
    return _response(status)


@router.delete(
    "/{name}",
    operation_id="removeCredential",
    status_code=204,
    responses=problems(404),
    dependencies=[Depends(no_request_body)],
)
def remove_one(name: CredentialName, session: DbSession, workspace_id: CurrentWorkspaceId) -> None:
    """Remove the stored value, if there is one. The environment's value is not touched."""
    remove_credential(session, workspace_id=workspace_id, name=name)
