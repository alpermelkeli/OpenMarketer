"""Tests for sealing a credential under the installation's key and opening it again."""

import base64
import dataclasses
import os
import uuid

import pytest

from openmarketer_core.credentials.cipher import (
    KEY_BYTES,
    NONCE_BYTES,
    CredentialCipher,
    EncryptionKeyInvalid,
    SealedValueDoesNotOpen,
)

WORKSPACE = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER_WORKSPACE = uuid.UUID("22222222-2222-4222-8222-222222222222")
BOUND = {"workspace_id": WORKSPACE, "credential_name": "github_token"}
SECRET = "ghp_0123456789abcdefghij"


def new_key(length: int = KEY_BYTES) -> str:
    return base64.b64encode(os.urandom(length)).decode()


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(new_key())


def test_sealed_value_opens_to_what_was_sealed(cipher):
    assert cipher.open(cipher.seal(SECRET, **BOUND), **BOUND) == SECRET


def test_sealed_value_does_not_hold_the_value(cipher):
    sealed = cipher.seal(SECRET, **BOUND)
    assert SECRET.encode() not in sealed.ciphertext


def test_key_with_whitespace_around_it_is_the_same_key():
    key = new_key()
    sealed = CredentialCipher(key).seal(SECRET, **BOUND)
    assert CredentialCipher(f" {key}\n").open(sealed, **BOUND) == SECRET


def test_every_seal_takes_a_fresh_nonce_of_96_bits(cipher):
    first, second = cipher.seal(SECRET, **BOUND), cipher.seal(SECRET, **BOUND)
    assert len(first.nonce) == len(second.nonce) == NONCE_BYTES == 12
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext


def test_value_sealed_for_another_workspace_does_not_open(cipher):
    sealed = cipher.seal(SECRET, workspace_id=OTHER_WORKSPACE, credential_name="github_token")
    with pytest.raises(SealedValueDoesNotOpen):
        cipher.open(sealed, **BOUND)


def test_value_sealed_under_another_name_does_not_open(cipher):
    sealed = cipher.seal(SECRET, workspace_id=WORKSPACE, credential_name="gitlab_token")
    with pytest.raises(SealedValueDoesNotOpen):
        cipher.open(sealed, **BOUND)


def test_changed_ciphertext_does_not_open(cipher):
    sealed = cipher.seal(SECRET, **BOUND)
    changed = bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]
    with pytest.raises(SealedValueDoesNotOpen):
        cipher.open(dataclasses.replace(sealed, ciphertext=changed), **BOUND)


def test_changed_nonce_does_not_open(cipher):
    sealed = cipher.seal(SECRET, **BOUND)
    changed = bytes([sealed.nonce[0] ^ 1]) + sealed.nonce[1:]
    with pytest.raises(SealedValueDoesNotOpen):
        cipher.open(dataclasses.replace(sealed, nonce=changed), **BOUND)


@pytest.mark.parametrize("nonce", [b"", b"short", b"n" * 200], ids=["empty", "short", "long"])
def test_nonce_of_another_length_does_not_open_and_is_not_a_crash(cipher, nonce):
    sealed = cipher.seal(SECRET, **BOUND)
    with pytest.raises(SealedValueDoesNotOpen):
        cipher.open(dataclasses.replace(sealed, nonce=nonce), **BOUND)


def test_value_sealed_with_another_key_does_not_open(cipher):
    sealed = CredentialCipher(new_key()).seal(SECRET, **BOUND)
    with pytest.raises(SealedValueDoesNotOpen):
        cipher.open(sealed, **BOUND)


def test_value_that_does_not_open_says_nothing_of_the_row(cipher):
    sealed = cipher.seal(SECRET, workspace_id=OTHER_WORKSPACE, credential_name="github_token")
    with pytest.raises(SealedValueDoesNotOpen) as refused:
        cipher.open(sealed, **BOUND)
    assert str(refused.value) == "the sealed value does not open with this key for this credential"
    assert refused.value.__cause__ is None


@pytest.mark.parametrize("key", ["", "   \n"], ids=["empty", "only whitespace"])
def test_missing_key_is_refused_saying_what_to_set(key):
    with pytest.raises(EncryptionKeyInvalid, match="not set.*openssl rand -base64 32"):
        CredentialCipher(key)


@pytest.mark.parametrize(
    "key",
    ["not base64 at all!", "c2VjcmV0LWtleS1ieXRlcw", "sécret-key-bytes-0123456789abcdef"],
    ids=["other characters", "no padding", "not ASCII"],
)
def test_key_that_is_not_base64_is_refused_without_showing_it(key):
    with pytest.raises(
        EncryptionKeyInvalid, match="not base64.*openssl rand -base64 32"
    ) as refused:
        CredentialCipher(key)
    assert key not in str(refused.value)
    assert refused.value.__cause__ is None


@pytest.mark.parametrize("length", [16, 24, 31, 33, 64])
def test_key_that_is_not_32_bytes_is_refused_without_showing_it(length):
    key = new_key(length)
    with pytest.raises(EncryptionKeyInvalid, match=f"is {length} bytes, not 32") as refused:
        CredentialCipher(key)
    assert key not in str(refused.value)


def test_repr_never_shows_the_key():
    key = new_key()
    shown = repr(CredentialCipher(key))
    assert key not in shown
    assert repr(base64.b64decode(key)) not in shown
    assert shown == "CredentialCipher(key=<hidden>)"
