"""Which credentials an installation can set, and what an acceptable value is.

There are three, the secrets the code uses: the model provider's API key and
the access tokens of the two repository hosts. Each has an environment
variable, which is where it was configured before it could be stored.

A value ends up in an HTTP ``Authorization`` header and in the input of git's
credential helper, so it must not be able to break out of either: it is
printable ASCII without a space, of a bounded length. A refusal names the rule
and never repeats the value or a part of it.

``SealedValue`` is the shape a value has once encrypted; the cipher makes it
and the store keeps it.

This module is a list and a rule. It does not read the environment, the
database or a key, and it does not check a value against its provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_VALUE_LENGTH = 4096
_MAX_SHOWN_CHARACTERS = 60


class UnknownCredential(Exception):
    """No credential of the catalog has that name."""

    def __init__(self, name: str) -> None:
        super().__init__(f"there is no credential named {_shown(name)}")


class CredentialValueRefused(Exception):
    """The value is not one a credential can have; the message says which rule it broke."""


@dataclass(frozen=True)
class Credential:
    """One secret of the installation: its name and the environment variable that sets it."""

    name: str
    environment_variable: str


@dataclass(frozen=True)
class SealedValue:
    """An encrypted value: the nonce it was sealed with and the ciphertext."""

    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)


OPENROUTER_API_KEY = Credential("openrouter_api_key", "OPENROUTER_API_KEY")
GITHUB_TOKEN = Credential("github_token", "GITHUB_TOKEN")
GITLAB_TOKEN = Credential("gitlab_token", "GITLAB_TOKEN")

CREDENTIALS = (OPENROUTER_API_KEY, GITHUB_TOKEN, GITLAB_TOKEN)


def credential_named(name: str) -> Credential:
    """The credential of the catalog with that name; ``UnknownCredential`` when there is none."""
    for credential in CREDENTIALS:
        if credential.name == name:
            return credential
    raise UnknownCredential(name)


def acceptable_value(value: str) -> str:
    """``value`` without the whitespace around it, if it is one a credential can have.

    Raises ``CredentialValueRefused`` otherwise. A pasted key often ends with a
    newline, so whitespace around the value is dropped; whitespace inside it is
    a mistake and is refused.
    """
    stripped = value.strip()
    if not stripped:
        raise CredentialValueRefused("a credential cannot be empty")
    if len(stripped) > MAX_VALUE_LENGTH:
        raise CredentialValueRefused(f"a credential is at most {MAX_VALUE_LENGTH} characters long")
    if not all("!" <= character <= "~" for character in stripped):
        raise CredentialValueRefused(
            "a credential holds only printable ASCII characters, with no space,"
            " line break or control character inside it"
        )
    return stripped


def _shown(name: str) -> str:
    """A name a caller wrote, quoted on one line and short enough for a message."""
    quoted = repr(name)
    if len(quoted) <= _MAX_SHOWN_CHARACTERS:
        return quoted
    return quoted[: _MAX_SHOWN_CHARACTERS - 3] + "..."
