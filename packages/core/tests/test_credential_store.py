"""Tests for the credential store. They need PostgreSQL (see the root ``conftest.py``)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.credentials.catalog import SealedValue
from openmarketer_core.db.credentials import (
    StoredCredential,
    remove_stored_credential,
    sealed_credential,
    store_credential,
    stored_credentials,
)
from openmarketer_core.db.models import CredentialRecord, Workspace

OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
COLLEAGUE = uuid.UUID("22222222-2222-4222-8222-222222222222")
SEALED = SealedValue(nonce=b"first-nonce!", ciphertext=b"first ciphertext")
SEALED_AGAIN = SealedValue(nonce=b"other-nonce!", ciphertext=b"second ciphertext")
NAMES = ("github_token", "gitlab_token", "openrouter_api_key")


def new_workspace(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


@pytest.fixture
def workspace_id(session) -> uuid.UUID:
    return new_workspace(session)


def store(session, workspace_id, name="github_token", sealed=SEALED, set_by=OWNER):
    return store_credential(
        session, workspace_id=workspace_id, name=name, sealed=sealed, set_by=set_by
    )


def stored_names(session, workspace_id, names=NAMES) -> list[str]:
    stored = stored_credentials(session, workspace_id=workspace_id, names=names)
    return [credential.name for credential in stored]


def database_time(session) -> datetime:
    return session.scalar(select(func.now()))


def test_stored_value_is_read_back_as_it_was_sealed(session, workspace_id):
    store(session, workspace_id)
    assert sealed_credential(session, workspace_id=workspace_id, name="github_token") == SEALED


def test_storing_says_who_set_the_value_and_when_by_the_databases_clock(session, workspace_id):
    stored = store(session, workspace_id)
    assert stored == StoredCredential(
        name="github_token", set_at=database_time(session), set_by=OWNER
    )


def test_credential_that_was_never_stored_reads_as_none(session, workspace_id):
    assert sealed_credential(session, workspace_id=workspace_id, name="github_token") is None


def test_storing_again_replaces_the_value_and_who_set_it(session, workspace_id):
    store(session, workspace_id)
    replaced = store(session, workspace_id, sealed=SEALED_AGAIN, set_by=COLLEAGUE)
    assert sealed_credential(session, workspace_id=workspace_id, name="github_token") == (
        SEALED_AGAIN
    )
    assert replaced.set_by == COLLEAGUE
    assert stored_credentials(session, workspace_id=workspace_id, names=NAMES) == [replaced]


def test_storing_again_sets_the_time_again(session, workspace_id):
    store(session, workspace_id)
    session.execute(text("UPDATE credential SET set_at = '2020-01-01T00:00:00Z'"))
    replaced = store(session, workspace_id, sealed=SEALED_AGAIN)
    assert replaced.set_at == database_time(session)
    assert replaced.set_at > datetime(2020, 1, 2, tzinfo=UTC)


def test_each_name_has_its_own_value(session, workspace_id):
    store(session, workspace_id, name="github_token", sealed=SEALED)
    store(session, workspace_id, name="gitlab_token", sealed=SEALED_AGAIN)
    assert sealed_credential(session, workspace_id=workspace_id, name="github_token") == SEALED
    assert sealed_credential(session, workspace_id=workspace_id, name="gitlab_token") == (
        SEALED_AGAIN
    )


def test_stored_credentials_are_listed_by_name(session, workspace_id):
    for name in ("openrouter_api_key", "github_token"):
        store(session, workspace_id, name=name)
    assert stored_names(session, workspace_id) == ["github_token", "openrouter_api_key"]


def test_only_the_names_asked_for_are_listed(session, workspace_id):
    for name in ("github_token", "a_name_the_code_no_longer_knows"):
        store(session, workspace_id, name=name)
    assert stored_names(session, workspace_id) == ["github_token"]
    assert stored_names(session, workspace_id, names=()) == []


def test_removing_a_stored_value_says_there_was_one(session, workspace_id):
    store(session, workspace_id)
    assert remove_stored_credential(session, workspace_id=workspace_id, name="github_token")
    assert sealed_credential(session, workspace_id=workspace_id, name="github_token") is None
    assert stored_names(session, workspace_id) == []


def test_removing_what_was_never_stored_says_there_was_none(session, workspace_id):
    assert not remove_stored_credential(session, workspace_id=workspace_id, name="github_token")


def test_removing_one_credential_keeps_the_others(session, workspace_id):
    for name in ("github_token", "gitlab_token"):
        store(session, workspace_id, name=name)
    remove_stored_credential(session, workspace_id=workspace_id, name="github_token")
    assert stored_names(session, workspace_id) == ["gitlab_token"]


def test_credential_of_another_workspace_is_not_listed(session, workspace_id):
    store(session, new_workspace(session))
    assert stored_names(session, workspace_id) == []


def test_credential_of_another_workspace_is_not_read(session, workspace_id):
    store(session, new_workspace(session))
    assert sealed_credential(session, workspace_id=workspace_id, name="github_token") is None


def test_credential_of_another_workspace_is_not_removed(session, workspace_id):
    other = new_workspace(session)
    store(session, other)
    assert not remove_stored_credential(session, workspace_id=workspace_id, name="github_token")
    assert sealed_credential(session, workspace_id=other, name="github_token") == SEALED


def test_storing_in_one_workspace_does_not_replace_the_value_of_another(session, workspace_id):
    other = new_workspace(session)
    store(session, other, sealed=SEALED)
    store(session, workspace_id, sealed=SEALED_AGAIN)
    assert sealed_credential(session, workspace_id=other, name="github_token") == SEALED


def test_list_of_stored_credentials_never_reads_a_value(session, workspace_id, statements):
    store(session, workspace_id)
    statements.clear()
    assert stored_names(session, workspace_id) == ["github_token"]
    assert len(statements) == 1
    assert "nonce" not in statements[0]
    assert "ciphertext" not in statements[0]


def test_reading_a_sealed_value_is_one_statement(session, workspace_id, statements):
    store(session, workspace_id)
    statements.clear()
    sealed_credential(session, workspace_id=workspace_id, name="github_token")
    assert len(statements) == 1


def test_storing_waits_for_other_writes_in_the_workspace_first(session, workspace_id, statements):
    statements.clear()
    store(session, workspace_id)
    assert "FOR UPDATE" in statements[0]
    assert "workspace" in statements[0]


def test_removing_waits_for_other_writes_in_the_workspace_first(session, workspace_id, statements):
    statements.clear()
    remove_stored_credential(session, workspace_id=workspace_id, name="github_token")
    assert "FOR UPDATE" in statements[0]
    assert "workspace" in statements[0]


def test_credential_needs_a_workspace_that_exists(session):
    with pytest.raises(IntegrityError, match="fk_credential_workspace_id_workspace"):
        store(session, uuid.uuid4())


def test_credential_needs_who_set_it(session, workspace_id):
    session.add(
        CredentialRecord(
            workspace_id=workspace_id, name="github_token", nonce=b"n", ciphertext=b"c"
        )
    )
    with pytest.raises(IntegrityError, match="set_by"):
        session.flush()
