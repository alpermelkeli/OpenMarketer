"""Signing a request with OAuth 1.0a and HMAC-SHA1 (RFC 5849), as X and other platforms ask.

A signed request carries an ``Authorization: OAuth ...`` header whose signature covers the
method, the URL without its query, and every parameter: the query's, a form body's and the
header's own ``oauth_*`` ones. The key it is signed with is the app's secret and, once there
is one, the token's secret; neither is sent.

The nonce and the timestamp are given by the caller, so a signature can be checked against
a published example. Neither secret appears in ``repr``.

This module is the arithmetic only, with the standard library. It sends nothing, chooses no
nonce and reads no clock, and it does not sign a body that is not a form (whose parameters
the caller passes): such a body is not covered by an OAuth 1.0a signature.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

SIGNATURE_METHOD = "HMAC-SHA1"
VERSION = "1.0"
# RFC 5849, 3.6: every byte but these unreserved characters is percent-encoded.
_UNRESERVED = "-._~"
_DEFAULT_PORTS = {"http": 80, "https": 443}


@dataclass(frozen=True)
class OAuth1Client:
    """The app a request is signed for: its consumer key and secret."""

    key: str = field(repr=False)
    secret: str = field(repr=False)


@dataclass(frozen=True)
class OAuth1Token:
    """A token the request is signed with, a request token or an access token, and its secret."""

    token: str = field(repr=False)
    secret: str = field(repr=False)


def percent_encode(text: str) -> str:
    """``text`` as UTF-8, every byte but the unreserved characters as ``%XX`` in upper case."""
    return quote(text.encode("utf-8"), safe=_UNRESERVED)


def base_string_uri(url: str) -> str:
    """The URL as a signature covers it: scheme and host in lower case, no default port,
    no query and no fragment (RFC 5849, 3.4.1.2)."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parts.port}"
    return urlunsplit((scheme, host, parts.path or "/", "", ""))


def signature_base_string(method: str, url: str, parameters: Sequence[tuple[str, str]]) -> str:
    """What is signed: the method, the URL and the sorted, encoded parameters, joined by ``&``.

    ``parameters`` are every parameter of the request but ``oauth_signature``; the query of
    ``url`` is added to them.
    """
    every = [*parse_qsl(urlsplit(url).query, keep_blank_values=True), *parameters]
    encoded = sorted((percent_encode(name), percent_encode(value)) for name, value in every)
    normalized = "&".join(f"{name}={value}" for name, value in encoded)
    return "&".join(
        (method.upper(), percent_encode(base_string_uri(url)), percent_encode(normalized))
    )


def signing_key(client: OAuth1Client, token: OAuth1Token | None) -> str:
    """The app's secret and the token's secret, encoded and joined by ``&``; the second may
    be empty."""
    token_secret = token.secret if token is not None else ""
    return f"{percent_encode(client.secret)}&{percent_encode(token_secret)}"


def signature(base_string: str, key: str) -> str:
    """The HMAC-SHA1 of ``base_string`` under ``key``, in base64."""
    digest = hmac.new(key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def authorization_header(
    method: str,
    url: str,
    *,
    client: OAuth1Client,
    token: OAuth1Token | None,
    nonce: str,
    timestamp: int,
    parameters: Sequence[tuple[str, str]] = (),
    oauth_parameters: Mapping[str, str] | None = None,
) -> str:
    """The value of the ``Authorization`` header of a request signed for ``client``.

    ``parameters`` are the request's form-body parameters (the query's are read from
    ``url``). ``oauth_parameters`` are the other ``oauth_*`` ones a step of the flow sends,
    such as ``oauth_callback`` or ``oauth_verifier``.
    """
    oauth = {
        "oauth_consumer_key": client.key,
        "oauth_nonce": nonce,
        "oauth_signature_method": SIGNATURE_METHOD,
        "oauth_timestamp": str(timestamp),
        "oauth_version": VERSION,
        **(oauth_parameters or {}),
    }
    if token is not None:
        oauth["oauth_token"] = token.token
    base_string = signature_base_string(method, url, [*oauth.items(), *parameters])
    oauth["oauth_signature"] = signature(base_string, signing_key(client, token))
    fields = ", ".join(
        f'{percent_encode(name)}="{percent_encode(value)}"' for name, value in sorted(oauth.items())
    )
    return f"OAuth {fields}"
