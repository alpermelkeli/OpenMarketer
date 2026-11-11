"""Tests for the social account store. They need PostgreSQL (see the root ``conftest.py``)."""

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.credentials.catalog import SealedValue
from openmarketer_core.db.models import SocialAccountRecord, Workspace
from openmarketer_core.db.projects import create_project
from openmarketer_core.db.social_accounts import (
    SealedAccountTokens,
    connected_account,
    connected_accounts,
    remove_connected_account,
    sealed_account_tokens,
    store_connected_account,
)
from openmarketer_core.social_accounts.catalog import AccountDescription, SocialPlatform

OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
COLLEAGUE = uuid.UUID("22222222-2222-4222-8222-222222222222")
ACME = AccountDescription(
    account_id="42", username="acme", display_name="Acme", picture_url="https://x/a.png"
)
TOKENS = SealedAccountTokens(
    access_token=SealedValue(nonce=b"token-nonce!", ciphertext=b"token ciphertext"),
    access_secret=SealedValue(nonce=b"secret-nonce", ciphertext=b"secret ciphertext"),
)
OTHER_TOKENS = SealedAccountTokens(
    access_token=SealedValue(nonce=b"token-nonce2", ciphertext=b"token again"),
    access_secret=SealedValue(nonce=b"secret-nonc2", ciphertext=b"secret again"),
)
SEALED_COLUMNS = ("nonce", "ciphertext")


def new_workspace(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


def new_project(session: Session, workspace_id: uuid.UUID, name: str = "app") -> uuid.UUID:
    url = f"https://github.com/acme/{name}"
    return create_project(session, workspace_id=workspace_id, name=name, source_repo_url=url).id


@pytest.fixture
def workspace_id(session) -> uuid.UUID:
    return new_workspace(session)


@pytest.fixture
def project_id(session, workspace_id) -> uuid.UUID:
    return new_project(session, workspace_id)


def store(session, workspace_id, project_id, account=ACME, tokens=TOKENS, by=OWNER):
    return store_connected_account(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        platform=SocialPlatform.X,
        account=account,
        tokens=tokens,
        connected_by=by,
    )


def database_now(session):
    return session.scalar(select(func.now()))


def test_stored_account_is_listed_as_the_platform_described_it(session, workspace_id, project_id):
    stored = store(session, workspace_id, project_id)
    assert connected_accounts(session, workspace_id=workspace_id, project_id=project_id) == [stored]
    assert (stored.platform, stored.account_id, stored.username) == (SocialPlatform.X, "42", "acme")
    assert (stored.display_name, stored.picture_url) == ("Acme", "https://x/a.png")
    assert stored.connected_by == OWNER
    assert stored.connected_at == stored.updated_at == database_now(session)


def test_stored_tokens_are_read_back_as_they_were_sealed(session, workspace_id, project_id):
    store(session, workspace_id, project_id)
    tokens = sealed_account_tokens(
        session, workspace_id=workspace_id, project_id=project_id, platform=SocialPlatform.X
    )
    assert tokens == TOKENS


def test_same_account_authorized_again_is_updated_and_keeps_its_connection_time(
    session, workspace_id, project_id
):
    first = store(session, workspace_id, project_id)
    session.execute(text("UPDATE social_account SET connected_at = now() - interval '1 day'"))
    renamed = AccountDescription("42", "acme_hq", "Acme HQ", None)
    again = store(
        session, workspace_id, project_id, account=renamed, tokens=OTHER_TOKENS, by=COLLEAGUE
    )
    assert again.id == first.id
    assert (again.username, again.display_name, again.picture_url) == ("acme_hq", "Acme HQ", None)
    assert again.connected_by == COLLEAGUE
    assert again.connected_at < again.updated_at
    assert session.scalar(select(func.count()).select_from(SocialAccountRecord)) == 1
    assert (
        sealed_account_tokens(
            session, workspace_id=workspace_id, project_id=project_id, platform=SocialPlatform.X
        )
        == OTHER_TOKENS
    )


def test_another_account_replaces_the_project_s_account_on_the_platform(
    session, workspace_id, project_id
):
    store(session, workspace_id, project_id)
    session.execute(text("UPDATE social_account SET connected_at = now() - interval '1 day'"))
    another = AccountDescription("7", "b", "B", None)
    other = store(session, workspace_id, project_id, account=another)
    assert connected_accounts(session, workspace_id=workspace_id, project_id=project_id) == [other]
    assert other.connected_at == other.updated_at


def test_one_account_can_be_connected_to_two_projects(session, workspace_id, project_id):
    second = new_project(session, workspace_id, "second")
    store(session, workspace_id, project_id)
    store(session, workspace_id, second)
    for project in (project_id, second):
        assert len(connected_accounts(session, workspace_id=workspace_id, project_id=project)) == 1


def test_account_of_another_workspace_is_not_found(session, workspace_id, project_id):
    store(session, workspace_id, project_id)
    other = new_workspace(session)
    ids = {"workspace_id": other, "project_id": project_id}
    assert connected_accounts(session, **ids) == []
    assert connected_account(session, **ids, platform=SocialPlatform.X) is None
    assert sealed_account_tokens(session, **ids, platform=SocialPlatform.X) is None
    assert remove_connected_account(session, **ids, platform=SocialPlatform.X) is False
    assert connected_accounts(session, workspace_id=workspace_id, project_id=project_id) != []


def test_removed_account_is_gone_with_its_tokens(session, workspace_id, project_id):
    store(session, workspace_id, project_id)
    ids = {"workspace_id": workspace_id, "project_id": project_id, "platform": SocialPlatform.X}
    assert remove_connected_account(session, **ids) is True
    assert sealed_account_tokens(session, **ids) is None
    assert remove_connected_account(session, **ids) is False


def test_lists_never_read_a_sealed_token(session, workspace_id, project_id, statements):
    store(session, workspace_id, project_id)
    statements.clear()
    connected_accounts(session, workspace_id=workspace_id, project_id=project_id)
    connected_account(
        session, workspace_id=workspace_id, project_id=project_id, platform=SocialPlatform.X
    )
    assert len(statements) == 2
    for statement in statements:
        assert not [column for column in SEALED_COLUMNS if column in statement]


def test_storing_and_removing_wait_for_other_writes_in_the_workspace_first(
    session, workspace_id, project_id, statements
):
    statements.clear()
    store(session, workspace_id, project_id)
    remove_connected_account(
        session, workspace_id=workspace_id, project_id=project_id, platform=SocialPlatform.X
    )
    locks = [statement for statement in statements if "FOR UPDATE" in statement]
    assert len(locks) == 2
    assert statements[0] == locks[0]


def test_stored_account_does_not_show_its_tokens(session, workspace_id, project_id):
    stored = store(session, workspace_id, project_id)
    shown = repr(stored)
    for sealed in (TOKENS.access_token, TOKENS.access_secret):
        assert repr(sealed.ciphertext) not in shown
    assert repr(TOKENS.access_token.ciphertext) not in repr(TOKENS)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("platform", "'linkedin'"),
        ("account_id", "''"),
        ("username", "''"),
        ("display_name", "repeat('a', 201)"),
        ("picture_url", "repeat('a', 2049)"),
    ],
)
def test_database_refuses_what_the_catalog_does_not_allow(
    session, workspace_id, project_id, column, value
):
    store(session, workspace_id, project_id)
    with pytest.raises(IntegrityError, match=f"ck_social_account_{column}"):
        with session.begin_nested():
            session.execute(text(f"UPDATE social_account SET {column} = {value}"))
