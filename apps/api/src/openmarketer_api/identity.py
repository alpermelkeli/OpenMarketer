"""Who is calling: the current user and the workspace they work in.

There is no user table and no login yet. In single-user local mode the user is
whoever sits at the machine the API runs on, so a request counts as that user
only when it provably comes from that machine. Real authentication replaces
``current_user_id`` and ``current_workspace_id`` and nothing else.

The user is never read from a request body, a query or a header a caller can
set freely: approval is recorded under this identity.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from typing import Annotated

from fastapi import Depends, Request

from openmarketer_api.dependencies import DbSession
from openmarketer_core.db.evidence_store import local_workspace_id

# The one user of single-user local mode. A fixed identifier, so that what this
# user approved can be told apart from real accounts once those exist.
LOCAL_USER_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "local-user.openmarketer")

# The whole of a Host header that names this machine: one of three names and, optionally,
# a port in digits. Anything else a URL parser might still read a local name out of
# (a user part, a fragment, a path, a backslash) does not match.
_LOCAL_HOST = r"(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?"
_LOCAL_HOST_HEADER = re.compile(_LOCAL_HOST, re.IGNORECASE)
_LOCAL_ORIGIN = re.compile(rf"https?://{_LOCAL_HOST}", re.IGNORECASE)


class NotLocalRequest(Exception):
    """The request does not come from the machine the API runs on."""


def current_user_id(request: Request) -> uuid.UUID:
    """The user making the request. In local mode: the person at this machine."""
    if not _comes_from_this_machine(request):
        raise NotLocalRequest(
            "without a login the API serves only requests made on the machine it runs on"
        )
    return LOCAL_USER_ID


def current_workspace_id(session: DbSession) -> uuid.UUID:
    """The workspace the request works in. In local mode: the single local workspace."""
    return local_workspace_id(session)


CurrentUserId = Annotated[uuid.UUID, Depends(current_user_id)]
CurrentWorkspaceId = Annotated[uuid.UUID, Depends(current_workspace_id)]


def _comes_from_this_machine(request: Request) -> bool:
    """Whether the connection, the addressed host and the calling page are all local.

    The peer address alone is not enough: a web page open in the user's browser
    connects from this machine too. Such a page announces itself in ``Origin``,
    and a foreign name pointed at 127.0.0.1 (DNS rebinding) shows up in ``Host``.
    Both headers are matched whole against the few forms that name this
    machine, never parsed: a value that is missing, repeated or malformed is
    not local.
    """
    if request.client is None or not _is_loopback(request.client.host):
        return False
    # The header as it was sent, exactly once. ``request.url`` would fill in the
    # listening address when the header is missing or cannot be parsed.
    hosts = request.headers.getlist("host")
    if len(hosts) != 1 or not _LOCAL_HOST_HEADER.fullmatch(hosts[0]):
        return False
    origins = request.headers.getlist("origin")
    return all(_LOCAL_ORIGIN.fullmatch(origin) for origin in origins)


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
