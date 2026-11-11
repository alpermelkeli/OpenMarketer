"""A project's accounts on social platforms: connecting one, listing them, disconnecting one.

Connecting takes two requests. ``connect`` asks the platform where to send the person and
answers that URL; the dashboard sends the browser there. The platform sends the person back
to the dashboard's callback page, ``{DASHBOARD_URL}/connections/{platform}/callback``, built
here from the API's setting and never taken from the client, and that page posts what the
platform brought to ``callback``, which finishes the connection, or forgets it when the
person declined.

The callback's body is platform-neutral, as core's call is: ``state`` names the attempt and
``grant`` is what the person's agreement brought; a body without a grant says the person
declined. For X the dashboard maps ``oauth_token`` to ``state``, ``oauth_verifier`` to
``grant``, and ``denied`` (which X sends with the request token when the person cancels) to
``state`` without a grant. Either answer names the project the connection was started for,
so the dashboard can go back to it; an answer on another platform than the attempt's is not
found. Both are short-lived secrets, so the callback's router does not
quote a refused body back (``secret_bodies.py``).

No response holds a token, and none holds the row id of an account: a project has one
account per platform, so the platform names it. What a platform answered is never repeated
(``errors.py``). Who connected an account is the current user, never a field of the request.
Disconnecting forgets the token; it does not revoke it on the platform.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from openmarketer_api.dependencies import (
    AccountAuthorizations,
    DashboardUrl,
    DbSession,
    Environment,
    StoringCipher,
    no_request_body,
)
from openmarketer_api.errors import SocialAccountNotFound, problems
from openmarketer_api.identity import CurrentUserId, CurrentWorkspaceId
from openmarketer_api.secret_bodies import SecretBodyRoute
from openmarketer_core.db.social_accounts import ConnectedAccount
from openmarketer_core.social_accounts.catalog import SocialPlatform
from openmarketer_core.social_accounts.connection import (
    ConnectionAttemptNotFound,
    PlatformReadiness,
    abandon_connection,
    connectable_platforms,
    disconnect,
    finish_connection,
    list_accounts,
    start_connection,
)

router = APIRouter(prefix="/projects/{project_id}/social-accounts", tags=["social accounts"])
callback_router = APIRouter(
    prefix="/social-accounts", tags=["social accounts"], route_class=SecretBodyRoute
)


class ConnectedAccountResponse(BaseModel):
    """A project's account on a platform, as the platform described it; never its token."""

    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    platform: SocialPlatform
    account_id: str = Field(description="The platform's identifier of the account.")
    username: str
    display_name: str
    picture_url: str | None
    connected_by: uuid.UUID
    connected_at: datetime = Field(description="When this account was connected to the project.")
    updated_at: datetime = Field(description="When this account was last authorized.")


class MissingCredentialResponse(BaseModel):
    """A credential of the installation's app on a platform that is neither set nor stored."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="The credential's name in Settings, Credentials.")
    environment_variable: str


class ConnectablePlatformResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: SocialPlatform
    name: str = Field(description="The name a person knows the platform by.")
    ready: bool = Field(
        description=(
            "Whether the installation's app on the platform has its credentials, set or"
            " stored. Connecting while it has not is answered 409 `platform_app_not_set`."
        )
    )
    missing_credentials: list[MissingCredentialResponse] = Field(
        description="The app's credentials still to set; empty when `ready`."
    )


class SocialAccountsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: list[ConnectedAccountResponse] = Field(
        description="The project's connected accounts, one per platform at most, by platform."
    )
    platforms: list[ConnectablePlatformResponse] = Field(
        description="Every platform an account can be connected on."
    )
    can_connect: bool = Field(
        description=(
            "Whether this API has an `ENCRYPTION_KEY`, which an account's token is stored"
            " under. Without one, connecting is answered 409 `encryption_key_missing`."
        )
    )


class ConnectionStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    authorize_url: str = Field(
        description="The platform's page where the person agrees; send the browser there."
    )


class ConnectionCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # No length or pattern is set on either: Pydantic's refusal would quote the value.
    state: SecretStr = Field(
        description=(
            "What the platform names the connection by. X: the `oauth_token` of the callback,"
            " or its `denied` when the person cancelled."
        )
    )
    grant: SecretStr | None = Field(
        default=None,
        description=(
            "What the person's agreement brought. X: the `oauth_verifier`. Left out when the"
            " person declined, which forgets the connection."
        ),
    )


class ConnectionOutcome(StrEnum):
    CONNECTED = "connected"
    DECLINED = "declined"


class ConnectionCallbackResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: ConnectionOutcome
    project_id: uuid.UUID = Field(
        description="The project the connection was started for, connected or declined."
    )
    account: ConnectedAccountResponse | None = Field(
        description="The account connected; null when the person declined."
    )


def _platform(readiness: PlatformReadiness) -> ConnectablePlatformResponse:
    return ConnectablePlatformResponse(
        platform=readiness.platform.platform,
        name=readiness.platform.name,
        ready=readiness.ready,
        missing_credentials=[
            MissingCredentialResponse(
                name=credential.name, environment_variable=credential.environment_variable
            )
            for credential in readiness.missing
        ],
    )


def _account(account: ConnectedAccount) -> ConnectedAccountResponse:
    return ConnectedAccountResponse(
        project_id=account.project_id,
        platform=account.platform,
        account_id=account.account_id,
        username=account.username,
        display_name=account.display_name,
        picture_url=account.picture_url,
        connected_by=account.connected_by,
        connected_at=account.connected_at,
        updated_at=account.updated_at,
    )


@router.get("", operation_id="listSocialAccounts", response_model=SocialAccountsResponse)
def list_all(
    project_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    environment: Environment,
    cipher: StoringCipher,
) -> SocialAccountsResponse:
    """The project's connected accounts, and every platform one can be connected on.

    404 for a project the workspace does not have.
    """
    accounts = list_accounts(session, workspace_id=workspace_id, project_id=project_id)
    platforms = connectable_platforms(session, environment, workspace_id=workspace_id)
    return SocialAccountsResponse(
        accounts=[_account(account) for account in accounts],
        platforms=[_platform(readiness) for readiness in platforms],
        can_connect=cipher is not None,
    )


@router.post(
    "/{platform}/connect",
    operation_id="connectSocialAccount",
    response_model=ConnectionStartResponse,
    responses=problems(400, 404, 409, 502),
    dependencies=[Depends(no_request_body)],
)
def connect(
    project_id: uuid.UUID,
    platform: SocialPlatform,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    environment: Environment,
    cipher: StoringCipher,
    authorizations: AccountAuthorizations,
    dashboard_url: DashboardUrl,
) -> ConnectionStartResponse:
    """Start connecting the project's account on the platform; where to send the person.

    The platform sends the person back to the dashboard's
    `/connections/{platform}/callback`, which must be registered in the platform's app.
    404 for a project the workspace does not have; 409 without an `ENCRYPTION_KEY` or while
    a credential of the platform's app is not set; 400 when the platform refuses (often a
    wrong app key, or a callback URL the app does not allow); 502 when it cannot be reached.
    """
    authorize_url = start_connection(
        session,
        environment,
        workspace_id=workspace_id,
        project_id=project_id,
        platform=platform,
        callback_url=f"{dashboard_url}/connections/{platform.value}/callback",
        authorization=authorizations[platform],
        cipher=cipher,
    )
    return ConnectionStartResponse(authorize_url=authorize_url)


@callback_router.post(
    "/{platform}/callback",
    operation_id="finishSocialAccountConnection",
    response_model=ConnectionCallbackResponse,
    responses=problems(400, 404, 409, 410, 502),
)
def callback(
    platform: SocialPlatform,
    body: ConnectionCallbackRequest,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    user: CurrentUserId,
    environment: Environment,
    cipher: StoringCipher,
    authorizations: AccountAuthorizations,
) -> ConnectionCallbackResponse:
    """Finish the connection the platform answered, or forget it when the person declined.

    404 when the workspace started no such connection on this platform, or it was finished
    or declined already, 410 when it was started too long ago (both: connect again); 409
    without an `ENCRYPTION_KEY`, when a credential of the platform's app is not set, or when
    a secret of the connection can no longer be read; 400 when the platform refuses the
    grant; 502 when it cannot be reached.
    """
    state = body.state.get_secret_value()
    if body.grant is None:
        project_id = abandon_connection(
            session, workspace_id=workspace_id, platform=platform, state=state
        )
        if project_id is None:
            raise ConnectionAttemptNotFound
        return ConnectionCallbackResponse(
            outcome=ConnectionOutcome.DECLINED, project_id=project_id, account=None
        )
    account = finish_connection(
        session,
        environment,
        workspace_id=workspace_id,
        platform=platform,
        state=state,
        grant=body.grant.get_secret_value(),
        connected_by=user,
        authorization=authorizations[platform],
        cipher=cipher,
    )
    return ConnectionCallbackResponse(
        outcome=ConnectionOutcome.CONNECTED,
        project_id=account.project_id,
        account=_account(account),
    )


@router.delete(
    "/{platform}",
    operation_id="disconnectSocialAccount",
    status_code=204,
    responses=problems(404),
    dependencies=[Depends(no_request_body)],
)
def remove(
    project_id: uuid.UUID,
    platform: SocialPlatform,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
) -> None:
    """Forget the project's account on the platform and its token.

    404 for a project the workspace does not have, or when no account is connected on the
    platform. The token is not revoked on the platform.
    """
    if not disconnect(session, workspace_id=workspace_id, project_id=project_id, platform=platform):
        raise SocialAccountNotFound("this project has no account connected on that platform")
