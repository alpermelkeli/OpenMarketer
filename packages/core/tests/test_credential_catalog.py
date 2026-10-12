"""Tests for the catalog of credentials, the rule for a value and what the package imports."""

import ast
from pathlib import Path

import pytest

from openmarketer_core import credentials
from openmarketer_core.credentials import catalog
from openmarketer_core.credentials.catalog import (
    CREDENTIALS,
    GITHUB_TOKEN,
    GITLAB_TOKEN,
    MAX_VALUE_LENGTH,
    OPENROUTER_API_KEY,
    CredentialValueRefused,
    SealedValue,
    UnknownCredential,
    acceptable_value,
    credential_named,
)

SECRET = "sk-or-v1-0123456789abcdef"


def test_catalog_holds_the_three_credentials_and_their_environment_variables():
    assert {c.name: c.environment_variable for c in CREDENTIALS} == {
        "openrouter_api_key": "OPENROUTER_API_KEY",
        "github_token": "GITHUB_TOKEN",
        "gitlab_token": "GITLAB_TOKEN",
    }
    assert CREDENTIALS == (OPENROUTER_API_KEY, GITHUB_TOKEN, GITLAB_TOKEN)


@pytest.mark.parametrize("credential", CREDENTIALS, ids=lambda credential: credential.name)
def test_credential_is_found_by_its_name(credential):
    assert credential_named(credential.name) is credential


@pytest.mark.parametrize("name", ["", "GITHUB_TOKEN", "github_token ", "gitlab_host", "stripe_key"])
def test_name_that_is_not_in_the_catalog_is_refused(name):
    with pytest.raises(UnknownCredential):
        credential_named(name)


def test_unknown_name_is_not_repeated_in_the_message():
    with pytest.raises(UnknownCredential) as refused:
        credential_named(SECRET)
    assert str(refused.value) == "there is no such credential"


def test_acceptable_value_is_returned_as_it_is():
    assert acceptable_value(SECRET) == SECRET


def test_every_printable_ascii_character_but_the_space_is_accepted():
    value = "".join(chr(code) for code in range(0x21, 0x7F))
    assert acceptable_value(value) == value


@pytest.mark.parametrize("pasted", [SECRET + "\n", SECRET + "\r\n", "  " + SECRET + "\t"])
def test_whitespace_around_a_value_is_dropped(pasted):
    assert acceptable_value(pasted) == SECRET


def test_value_of_the_longest_length_is_accepted():
    assert acceptable_value("a" * MAX_VALUE_LENGTH) == "a" * MAX_VALUE_LENGTH


REFUSED = {
    "empty": ("", "empty"),
    "only whitespace": (" \n\t", "empty"),
    "too long": ("a" * (MAX_VALUE_LENGTH + 1), "at most 4096 characters"),
    "space inside": ("sk-or-v1 0123456789", "printable ASCII"),
    "line break inside": ("sk-or-v1\nhost=evil.example", "printable ASCII"),
    "carriage return inside": ("sk-or-v1\rX-Header: 1", "printable ASCII"),
    "control character": ("sk-or-v1\x000123456789", "printable ASCII"),
    "delete character": ("sk-or-v1\x7f0123456789", "printable ASCII"),
    "not ASCII": ("sk-or-v1-gizlí-anahtar", "printable ASCII"),
    "no-break space inside": ("sk-or-v1 0123456789", "printable ASCII"),
}


@pytest.mark.parametrize(("value", "rule"), REFUSED.values(), ids=REFUSED.keys())
def test_value_that_breaks_the_rule_is_refused_naming_the_rule(value, rule):
    with pytest.raises(CredentialValueRefused, match=rule):
        acceptable_value(value)


@pytest.mark.parametrize(
    "value",
    ["tok3n-PART one", "tok3n-PART\none", "tok3n-PART-ö", "tok3n-PART" * 500],
    ids=["space", "line break", "not ASCII", "too long"],
)
def test_refusal_never_repeats_the_value_or_a_part_of_it(value):
    with pytest.raises(CredentialValueRefused) as refused:
        acceptable_value(value)
    assert "tok3n" not in str(refused.value)
    assert "PART" not in str(refused.value)
    assert refused.value.__cause__ is None


def test_sealed_value_is_not_shown_by_its_repr():
    sealed = SealedValue(nonce=b"nonce-bytes!", ciphertext=b"ciphertext-bytes")
    assert "nonce-bytes" not in repr(sealed)
    assert "ciphertext-bytes" not in repr(sealed)


# ------------------------------------------------------ what the package imports
def imported_names(source_file: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source_file.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_package_init_imports_nothing():
    assert imported_names(Path(credentials.__file__)) == set()


def test_catalog_imports_only_the_standard_library():
    assert imported_names(Path(catalog.__file__)) == {"__future__", "dataclasses"}


def test_only_the_cipher_imports_cryptography():
    core = Path(credentials.__file__).parents[1]
    importing = {
        str(source_file.relative_to(core))
        for source_file in core.rglob("*.py")
        if any(
            name == "cryptography" or name.startswith("cryptography.")
            for name in imported_names(source_file)
        )
    }
    assert importing == {"credentials/cipher.py"}
