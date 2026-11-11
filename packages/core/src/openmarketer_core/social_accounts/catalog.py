"""Which social platforms a project can connect an account of, and what each one needs.

A platform is connected through the installation's own app on it: the person who runs
OpenMarketer registers one with the platform and stores its key and secret as two
credentials of ``credentials/catalog.py``. Only X can be connected so far.

The bounds below are what the database keeps of what a platform says about an account and
of the name it gives a connection attempt. That text is the platform's, so it is data: an
adapter keeps it within these bounds, and the table's CHECKs hold them too.

This module is a list. It does not read the environment or the database, and it does not
know how a platform's authorization works: that is the platform's adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from openmarketer_core.credentials.catalog import X_API_KEY, X_API_SECRET, Credential

MAX_ACCOUNT_ID_LENGTH = 64
MAX_USERNAME_LENGTH = 100
MAX_DISPLAY_NAME_LENGTH = 200
MAX_PICTURE_URL_LENGTH = 2048
# What a platform names a connection attempt by when it answers (the OAuth 1.0a request token).
MAX_STATE_LENGTH = 512


class SocialPlatform(StrEnum):
    """A platform an account can be connected on, by its slug."""

    X = "x"


@dataclass(frozen=True)
class AccountDescription:
    """What a platform says about an account, within the bounds above.

    ``account_id`` is the platform's identifier of the account, which does not change;
    the others are what the person sees and may change on the platform.
    """

    account_id: str
    username: str
    display_name: str
    picture_url: str | None


class UnknownPlatform(Exception):
    """No platform of the catalog has that slug; the message does not repeat it."""

    def __init__(self) -> None:
        super().__init__("there is no such platform to connect")


@dataclass(frozen=True)
class ConnectablePlatform:
    """A platform, the name a person knows it by, and the credentials of the installation's app.

    ``app_key`` and ``app_secret`` identify the app the platform's users authorize: an OAuth
    1.0a consumer key and secret, or an OAuth 2 client id and secret.
    """

    platform: SocialPlatform
    name: str
    app_key: Credential
    app_secret: Credential


X = ConnectablePlatform(
    platform=SocialPlatform.X, name="X", app_key=X_API_KEY, app_secret=X_API_SECRET
)

PLATFORMS = (X,)


def connectable_platform(slug: str) -> ConnectablePlatform:
    """The platform of the catalog with that slug; ``UnknownPlatform`` when there is none."""
    for connectable in PLATFORMS:
        if connectable.platform.value == slug:
            return connectable
    raise UnknownPlatform
