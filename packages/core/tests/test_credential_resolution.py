"""Tests for which value a credential has, its status, and setting or removing a stored one.

They need PostgreSQL (see the root ``conftest.py``). Keys are made up in the test.
"""

import base64
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from openmarketer_core.credentials.catalog import (
    CREDENTIALS,
    GITHUB_TOKEN,
    GITLAB_TOKEN,
    OPENROUTER_API_KEY,
    CredentialValueRefused,
    UnknownCredential,
)
from openmarketer_core.credentials.cipher import CredentialCipher
from openmarketer_core.credentials.resolution import (
    CredentialSetByEnvironment,
    CredentialSource,
    CredentialStatus,
    CredentialUnreadable,
    EncryptionKeyMissing,
    credential_statuses,
    credential_value,
    remove_credential,
    set_credential,
)
from openmarketer_core.db.credentials import sealed_credential, stored_credentials
from openmarketer_core.db.models import Workspace

OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
STORED_TOKEN = "ghp_stored0123456789"
ENVIRONMENT_TOKEN = "ghp_environment0123456789"
NO_ENVIRONMENT: dict[str, str] = {}


def new_workspace(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


@pytest.fixture
def workspace_id(session) -> uuid.UUID:
    return new_workspace(session)


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(base64.b64encode(os.urandom(32)).decode())


def store(session, workspace_id, cipher, name="github_token", value=STORED_TOKEN):
    return set_credential(
        session,
        NO_ENVIRONMENT,
        workspace_id=workspace_id,
        name=name,
        value=value,
        set_by=OWNER,
        cipher=cipher,
    )


def value_of(session, environ, workspace_id, cipher, credential=GITHUB_TOKEN):
    return credential_value(
        session, environ, workspace_id=workspace_id, credential=credential, cipher=cipher
    )


def status_of(session, environ, workspace_id, credential=GITHUB_TOKEN) -> CredentialStatus:
    statuses = credential_statuses(session, environ, workspace_id=workspace_id)
    return next(status for status in statuses if status.credential == credential)


# ------------------------------------------------------------- the value to use
def test_stored_value_is_used_when_the_environment_sets_none(session, workspace_id, cipher):
    store(session, workspace_id, cipher)
    assert value_of(session, NO_ENVIRONMENT, workspace_id, cipher) == STORED_TOKEN


def test_environment_wins_over_the_stored_value(session, workspace_id, cipher):
    store(session, workspace_id, cipher)
    environ = {"GITHUB_TOKEN": ENVIRONMENT_TOKEN}
    assert value_of(session, environ, workspace_id, cipher) == ENVIRONMENT_TOKEN


def test_environment_value_is_used_when_nothing_is_stored(session, workspace_id, cipher):
    environ = {"GITHUB_TOKEN": ENVIRONMENT_TOKEN}
    assert value_of(session, environ, workspace_id, cipher) == ENVIRONMENT_TOKEN


def test_empty_environment_variable_falls_back_to_the_stored_value(session, workspace_id, cipher):
    store(session, workspace_id, cipher)
    assert value_of(session, {"GITHUB_TOKEN": ""}, workspace_id, cipher) == STORED_TOKEN


def test_whitespace_in_the_environment_variable_falls_back_to_the_stored_value(
    session, workspace_id, cipher
):
    store(session, workspace_id, cipher)
    assert value_of(session, {"GITHUB_TOKEN": " \t\n"}, workspace_id, cipher) == STORED_TOKEN


def test_environment_value_is_used_without_the_whitespace_around_it(session, workspace_id, cipher):
    environ = {"GITHUB_TOKEN": f"  {ENVIRONMENT_TOKEN}\n"}
    assert value_of(session, environ, workspace_id, cipher) == ENVIRONMENT_TOKEN


def test_environment_variable_of_another_credential_sets_nothing(session, workspace_id, cipher):
    environ = {"GITLAB_TOKEN": ENVIRONMENT_TOKEN, "github_token": ENVIRONMENT_TOKEN}
    assert value_of(session, environ, workspace_id, cipher) is None


def test_credential_set_nowhere_has_no_value(session, workspace_id, cipher):
    assert value_of(session, NO_ENVIRONMENT, workspace_id, cipher) is None


def test_credential_set_nowhere_has_no_value_without_a_cipher_either(session, workspace_id):
    assert value_of(session, NO_ENVIRONMENT, workspace_id, cipher=None) is None


def test_value_stored_in_another_workspace_is_not_used(session, workspace_id, cipher):
    store(session, new_workspace(session), cipher)
    assert value_of(session, NO_ENVIRONMENT, workspace_id, cipher) is None


def test_value_stored_under_another_name_is_not_used(session, workspace_id, cipher):
    store(session, workspace_id, cipher, name="gitlab_token")
    assert value_of(session, NO_ENVIRONMENT, workspace_id, cipher) is None


def test_stored_value_without_a_cipher_is_unreadable(session, workspace_id, cipher):
    store(session, workspace_id, cipher)
    with pytest.raises(CredentialUnreadable, match="github_token.*ENCRYPTION_KEY.*set the") as e:
        value_of(session, NO_ENVIRONMENT, workspace_id, cipher=None)
    assert e.value.credential == GITHUB_TOKEN


def test_stored_value_under_a_different_key_is_unreadable(session, workspace_id, cipher):
    store(session, workspace_id, cipher)
    another_key = CredentialCipher(base64.b64encode(os.urandom(32)).decode())
    with pytest.raises(CredentialUnreadable, match="github_token.*ENCRYPTION_KEY") as e:
        value_of(session, NO_ENVIRONMENT, workspace_id, another_key)
    assert STORED_TOKEN not in str(e.value)
    assert e.value.__cause__ is None


def test_row_copied_to_another_workspace_is_unreadable_there(session, workspace_id, cipher):
    store(session, workspace_id, cipher)
    other = new_workspace(session)
    session.execute(
        text(
            "INSERT INTO credential (workspace_id, name, nonce, ciphertext, set_by)"
            " SELECT :other, name, nonce, ciphertext, set_by FROM credential"
            " WHERE workspace_id = :workspace_id"
        ),
        {"other": other, "workspace_id": workspace_id},
    )
    with pytest.raises(CredentialUnreadable):
        value_of(session, NO_ENVIRONMENT, other, cipher)


def test_row_copied_to_another_name_is_unreadable_under_it(session, workspace_id, cipher):
    store(session, workspace_id, cipher)
    session.execute(text("UPDATE credential SET name = 'gitlab_token'"))
    with pytest.raises(CredentialUnreadable, match="gitlab_token"):
        value_of(session, NO_ENVIRONMENT, workspace_id, cipher, credential=GITLAB_TOKEN)


def test_unreadable_stored_value_does_not_matter_while_the_environment_sets_the_credential(
    session, workspace_id, cipher
):
    store(session, workspace_id, cipher)
    environ = {"GITHUB_TOKEN": ENVIRONMENT_TOKEN}
    assert value_of(session, environ, workspace_id, cipher=None) == ENVIRONMENT_TOKEN


def test_value_from_the_environment_is_read_without_the_database(
    session, workspace_id, cipher, statements
):
    statements.clear()
    value_of(session, {"GITHUB_TOKEN": ENVIRONMENT_TOKEN}, workspace_id, cipher)
    assert statements == []


# ------------------------------------------------------------------ the statuses
def test_statuses_cover_the_catalog_in_its_order(session, workspace_id):
    statuses = credential_statuses(session, NO_ENVIRONMENT, workspace_id=workspace_id)
    assert tuple(status.credential for status in statuses) == CREDENTIALS


def test_status_of_a_credential_set_nowhere(session, workspace_id):
    assert status_of(session, NO_ENVIRONMENT, workspace_id) == CredentialStatus(
        credential=GITHUB_TOKEN, source=CredentialSource.NOT_SET, stored=None
    )


def test_status_of_a_stored_credential_says_who_set_it_and_when(session, workspace_id, cipher):
    stored = store(session, workspace_id, cipher).stored
    assert stored is not None
    assert (stored.name, stored.set_by) == ("github_token", OWNER)
    assert status_of(session, NO_ENVIRONMENT, workspace_id) == CredentialStatus(
        credential=GITHUB_TOKEN, source=CredentialSource.STORED, stored=stored
    )


def test_status_of_a_credential_set_by_the_environment(session, workspace_id):
    environ = {"GITHUB_TOKEN": ENVIRONMENT_TOKEN}
    assert status_of(session, environ, workspace_id) == CredentialStatus(
        credential=GITHUB_TOKEN, source=CredentialSource.ENVIRONMENT, stored=None
    )


def test_status_of_a_stored_credential_that_the_environment_overrides(
    session, workspace_id, cipher
):
    stored = store(session, workspace_id, cipher).stored
    environ = {"GITHUB_TOKEN": ENVIRONMENT_TOKEN}
    assert status_of(session, environ, workspace_id) == CredentialStatus(
        credential=GITHUB_TOKEN, source=CredentialSource.ENVIRONMENT, stored=stored
    )


def test_empty_environment_variable_does_not_count_as_set(session, workspace_id):
    status = status_of(session, {"GITHUB_TOKEN": ""}, workspace_id)
    assert status.source is CredentialSource.NOT_SET


def test_whitespace_in_the_environment_variable_does_not_count_as_set(session, workspace_id):
    status = status_of(session, {"GITHUB_TOKEN": " \t\n"}, workspace_id)
    assert status.source is CredentialSource.NOT_SET


def test_stored_credential_is_the_one_in_use_beside_whitespace_in_the_environment(
    session, workspace_id, cipher
):
    store(session, workspace_id, cipher)
    status = status_of(session, {"GITHUB_TOKEN": " "}, workspace_id)
    assert status.source is CredentialSource.STORED


def test_each_credential_has_its_own_status(session, workspace_id, cipher):
    store(session, workspace_id, cipher, name="gitlab_token")
    statuses = credential_statuses(
        session, {"OPENROUTER_API_KEY": "sk-or-v1-abc"}, workspace_id=workspace_id
    )
    assert {status.credential: status.source for status in statuses} == {
        OPENROUTER_API_KEY: CredentialSource.ENVIRONMENT,
        GITHUB_TOKEN: CredentialSource.NOT_SET,
        GITLAB_TOKEN: CredentialSource.STORED,
    }


def test_statuses_do_not_show_what_another_workspace_stored(session, workspace_id, cipher):
    store(session, new_workspace(session), cipher)
    assert status_of(session, NO_ENVIRONMENT, workspace_id).source is CredentialSource.NOT_SET


def test_statuses_never_hold_a_value(session, workspace_id, cipher):
    store(session, workspace_id, cipher)
    statuses = credential_statuses(
        session, {"GITLAB_TOKEN": ENVIRONMENT_TOKEN}, workspace_id=workspace_id
    )
    assert STORED_TOKEN not in repr(statuses)
    assert ENVIRONMENT_TOKEN not in repr(statuses)


def test_statuses_are_one_statement_that_reads_no_value(session, workspace_id, cipher, statements):
    store(session, workspace_id, cipher)
    statements.clear()
    credential_statuses(session, NO_ENVIRONMENT, workspace_id=workspace_id)
    assert len(statements) == 1
    assert "nonce" not in statements[0]
    assert "ciphertext" not in statements[0]


# ---------------------------------------------------------------- set and remove
def test_set_credential_is_stored_encrypted(session, workspace_id, cipher):
    store(session, workspace_id, cipher)
    sealed = sealed_credential(session, workspace_id=workspace_id, name="github_token")
    assert sealed is not None
    assert STORED_TOKEN.encode() not in sealed.ciphertext


def test_setting_says_the_value_is_now_the_stored_one(session, workspace_id, cipher):
    status = store(session, workspace_id, cipher)
    assert (status.credential, status.source) == (GITHUB_TOKEN, CredentialSource.STORED)
    assert status.stored is not None
    assert status.stored.set_by == OWNER


def test_setting_again_replaces_the_value(session, workspace_id, cipher):
    store(session, workspace_id, cipher)
    store(session, workspace_id, cipher, value="ghp_replacement")
    assert value_of(session, NO_ENVIRONMENT, workspace_id, cipher) == "ghp_replacement"


def test_whitespace_around_a_set_value_is_not_stored(session, workspace_id, cipher):
    store(session, workspace_id, cipher, value=f"  {STORED_TOKEN}\n")
    assert value_of(session, NO_ENVIRONMENT, workspace_id, cipher) == STORED_TOKEN


def test_setting_a_name_that_is_not_in_the_catalog_is_refused(session, workspace_id, cipher):
    with pytest.raises(UnknownCredential):
        store(session, workspace_id, cipher, name="gitlab_host")


def test_setting_a_credential_the_environment_sets_is_refused(session, workspace_id, cipher):
    with pytest.raises(CredentialSetByEnvironment, match="github_token.*GITHUB_TOKEN") as refused:
        set_credential(
            session,
            {"GITHUB_TOKEN": ENVIRONMENT_TOKEN},
            workspace_id=workspace_id,
            name="github_token",
            value=STORED_TOKEN,
            set_by=OWNER,
            cipher=cipher,
        )
    assert refused.value.credential == GITHUB_TOKEN
    assert ENVIRONMENT_TOKEN not in str(refused.value)
    assert STORED_TOKEN not in str(refused.value)
    assert stored_credentials(session, workspace_id=workspace_id, names=["github_token"]) == []


def test_setting_is_accepted_beside_whitespace_in_the_environment_variable(
    session, workspace_id, cipher
):
    environ = {"GITHUB_TOKEN": " "}
    status = set_credential(
        session,
        environ,
        workspace_id=workspace_id,
        name="github_token",
        value=STORED_TOKEN,
        set_by=OWNER,
        cipher=cipher,
    )
    assert status.source is CredentialSource.STORED
    assert value_of(session, environ, workspace_id, cipher) == STORED_TOKEN


def test_setting_a_value_that_breaks_the_rule_is_refused_and_stores_nothing(
    session, workspace_id, cipher
):
    with pytest.raises(CredentialValueRefused) as refused:
        store(session, workspace_id, cipher, value="ghp_two words")
    assert "ghp_two" not in str(refused.value)
    assert stored_credentials(session, workspace_id=workspace_id, names=["github_token"]) == []


def test_setting_without_a_cipher_is_refused_saying_what_is_needed(session, workspace_id):
    with pytest.raises(EncryptionKeyMissing, match="ENCRYPTION_KEY is needed to store"):
        store(session, workspace_id, cipher=None)
    assert stored_credentials(session, workspace_id=workspace_id, names=["github_token"]) == []


def test_removing_a_stored_credential_leaves_it_without_a_value(session, workspace_id, cipher):
    store(session, workspace_id, cipher)
    assert remove_credential(session, workspace_id=workspace_id, name="github_token")
    assert value_of(session, NO_ENVIRONMENT, workspace_id, cipher) is None
    assert status_of(session, NO_ENVIRONMENT, workspace_id).source is CredentialSource.NOT_SET


def test_removing_a_credential_that_is_not_stored_says_there_was_none(session, workspace_id):
    assert not remove_credential(session, workspace_id=workspace_id, name="github_token")


def test_removing_a_name_that_is_not_in_the_catalog_is_refused(session, workspace_id):
    with pytest.raises(UnknownCredential):
        remove_credential(session, workspace_id=workspace_id, name="gitlab_host")


def test_removing_does_not_touch_what_another_workspace_stored(session, workspace_id, cipher):
    other = new_workspace(session)
    store(session, other, cipher)
    assert not remove_credential(session, workspace_id=workspace_id, name="github_token")
    assert value_of(session, NO_ENVIRONMENT, other, cipher) == STORED_TOKEN
