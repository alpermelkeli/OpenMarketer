"""Tests for the credentials an analysis runs with: resolved for its workspace, at each attempt.

The activities are the ones the worker registers, run as plain functions against PostgreSQL.
The credentials are resolved for real (``WorkspaceCredentials``, the analyzer's tokens) and
the chat model is the real client, on a transport that answers in place of the provider and
keeps the key it was sent. Keys and tokens are made up.
"""

from collections.abc import Awaitable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker
from temporalio.exceptions import ApplicationError

from openmarketer_core.credentials.catalog import GITHUB_TOKEN, OPENROUTER_API_KEY
from openmarketer_core.db.checkpoint_schema import CHECKPOINT_TABLES_OF_THREADS
from openmarketer_core.db.session import DatabaseError, transaction
from openmarketer_core.llm import RouterChatModel
from openmarketer_core.llm_config import ModelRouter
from openmarketer_worker.agent_runtime.vocabulary import DATABASE_UNAVAILABLE
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.repository_analyzer import steps
from openmarketer_worker.repository_analyzer.repository_tokens import WorkspaceRepositoryTokens

MODELS = Path(__file__).resolve().parents[4] / "config" / "models.yaml"
STORED_KEY = "sk-stored-0123456789"
ENVIRONMENT_KEY = "sk-environment-0123456789"
STORED_TOKEN = "ghp_stored0123456789"
KEY_NOT_SET = (
    "the model provider's API key is not set: set it in the dashboard's Settings,"
    " or with the environment variable OPENROUTER_API_KEY"
)


async def refused(work: Awaitable[object]) -> ApplicationError:
    with pytest.raises(ApplicationError) as failure:
        await work
    return failure.value


@pytest.fixture
def keys_sent() -> list[str]:
    """The ``Authorization`` header of each request that reached the provider."""
    return []


@pytest.fixture
def worker_with(analyzer_with, sessions, cipher, keys_sent, turns):
    """The analyzer of a worker with that environment, whose provider accepts any profile."""

    def provider(request: httpx.Request) -> httpx.Response:
        keys_sent.append(request.headers["authorization"])
        message = {"role": "assistant", "content": None, "tool_calls": turns.submits}
        return httpx.Response(200, json={"model": "fake/model", "choices": [{"message": message}]})

    def worker(environ: dict[str, str], cipher=cipher, credentials=None):
        credentials = credentials or WorkspaceCredentials(sessions, environ, cipher)
        router = ModelRouter.from_file(MODELS, env=environ)
        return analyzer_with(
            new_model=lambda workspace_id: RouterChatModel(
                router=router,
                transport=httpx.MockTransport(provider),
                api_key=credentials.model_key(workspace_id, router),
            ),
            tokens_of=WorkspaceRepositoryTokens(credentials, environ).of,
        )

    return worker


# ------------------------------------------------------- the model provider's key
async def test_analysis_calls_the_model_with_the_key_stored_for_its_workspace(
    worker_with, database, started, cipher, keys_sent
):
    database.store_credential(started.workspace_id, OPENROUTER_API_KEY, STORED_KEY, cipher)
    await worker_with({}).to_the_end(started)
    assert keys_sent == [f"Bearer {STORED_KEY}"]


async def test_key_of_the_environment_wins_over_the_stored_one(
    worker_with, database, started, cipher, keys_sent
):
    database.store_credential(started.workspace_id, OPENROUTER_API_KEY, STORED_KEY, cipher)
    await worker_with({"OPENROUTER_API_KEY": ENVIRONMENT_KEY}).to_the_end(started)
    assert keys_sent == [f"Bearer {ENVIRONMENT_KEY}"]


async def test_analysis_without_a_key_anywhere_ends_saying_where_to_set_one(
    worker_with, started, intake, keys_sent
):
    failure = await refused(worker_with({}).step(started))
    assert (failure.type, failure.non_retryable) == (steps.ANALYSIS_FAILED, True)
    assert failure.message == KEY_NOT_SET
    assert failure.__cause__ is None
    # Before anything was cloned or sent.
    assert (intake.cloned, keys_sent) == ([], [])


async def test_key_stored_after_the_worker_was_built_is_used_by_the_next_attempt(
    worker_with, database, started, cipher, keys_sent
):
    worker = worker_with({})
    await refused(worker.step(started))
    database.store_credential(started.workspace_id, OPENROUTER_API_KEY, STORED_KEY, cipher)
    await worker.to_the_end(started)
    assert keys_sent == [f"Bearer {STORED_KEY}"]


async def test_key_that_was_replaced_while_the_run_waited_is_the_one_the_next_step_uses(
    worker_with, database, started, cipher, keys_sent
):
    worker = worker_with({})
    database.store_credential(started.workspace_id, OPENROUTER_API_KEY, STORED_KEY, cipher)
    cloned = await worker.cloned(started)
    database.store_credential(started.workspace_id, OPENROUTER_API_KEY, "sk-replaced-01234", cipher)
    await worker.step(started, cloned)
    assert keys_sent == ["Bearer sk-replaced-01234"]


@pytest.mark.parametrize("readable_with", ["no key", "another key"])
async def test_stored_key_that_cannot_be_read_ends_the_analysis_without_its_value(
    worker_with, database, started, cipher, another_cipher, keys_sent, readable_with
):
    database.store_credential(started.workspace_id, OPENROUTER_API_KEY, STORED_KEY, cipher)
    worker = worker_with({}, cipher=None if readable_with == "no key" else another_cipher)
    failure = await refused(worker.step(started))
    assert (failure.type, failure.non_retryable) == (steps.ANALYSIS_FAILED, True)
    assert failure.message == (
        "the stored value of openrouter_api_key cannot be read: set ENCRYPTION_KEY to the"
        " key it was stored with, or set the credential again"
    )
    assert failure.__cause__ is None
    assert keys_sent == []


async def test_database_that_fails_while_the_key_is_resolved_is_worth_another_attempt(
    worker_with, started, sessions
):
    class Unreachable(WorkspaceCredentials):
        def values(self, workspace_id, credentials):
            raise DatabaseError("connection is closed")

    worker = worker_with({}, credentials=Unreachable(sessions, {}, None))
    failure = await refused(worker.step(started))
    assert (failure.type, failure.non_retryable) == (DATABASE_UNAVAILABLE, False)
    assert failure.message == steps.ANALYSIS_TEXTS.database_failed


async def test_no_credential_is_in_what_the_activities_return_or_in_the_checkpoints(
    worker_with, database, started, cipher, sessions
):
    token = "ghp_environment0123456789"
    database.store_credential(started.workspace_id, OPENROUTER_API_KEY, STORED_KEY, cipher)
    worker = worker_with({"GITHUB_TOKEN": token})
    asked = await worker.step(started)
    cloned = await worker.act(asked.action)
    kept = checkpoints_as_text(sessions)
    done = await worker.step(started, cloned)
    assert str(started.run_id) in kept
    for shown in (repr((asked, cloned, done)), kept):
        for secret in (STORED_KEY, token):
            # A binary column is printed in hexadecimal.
            assert secret not in shown and secret.encode().hex() not in shown


def checkpoints_as_text(sessions: sessionmaker[Session]) -> str:
    """Every row of the checkpoint tables, as the database prints it."""
    with transaction(sessions) as session:
        return "\n".join(
            row
            for table in CHECKPOINT_TABLES_OF_THREADS
            for row in session.execute(text(f"SELECT t::text FROM {table} t")).scalars()
        )


# ----------------------------------------------------------- the repository tokens
async def test_stored_github_token_reaches_the_clone_of_a_repository_on_github(
    worker_with, database, cipher, intake
):
    run = database.requested_run("https://github.com/acme/app.git")
    database.store_credential(run.workspace_id, GITHUB_TOKEN, STORED_TOKEN, cipher)
    database.store_credential(run.workspace_id, OPENROUTER_API_KEY, STORED_KEY, cipher)
    worker = worker_with({})
    worker.start(run)
    await worker.cloned(run)
    (offered,) = intake.offered
    assert offered.token_for("https://github.com/acme/app.git") == STORED_TOKEN
    assert offered.token_for("https://example.com/acme/app.git") is None
    assert offered.token_for("https://github.com.example.com/acme/app.git") is None


async def test_token_of_one_workspace_is_not_offered_to_the_clone_of_anothers_run(
    worker_with, database, cipher, intake
):
    owner = database.new_workspace()
    database.store_credential(owner, GITHUB_TOKEN, STORED_TOKEN, cipher)
    run = database.requested_run("https://github.com/acme/app.git")
    worker = worker_with({"OPENROUTER_API_KEY": ENVIRONMENT_KEY})
    worker.start(run)
    await worker.cloned(run)
    (offered,) = intake.offered
    assert offered.token_for("https://github.com/acme/app.git") is None


async def test_commit_fetched_again_on_another_worker_is_fetched_with_the_stored_token(
    worker_with, database, cipher, intake
):
    run = database.requested_run("https://github.com/acme/app.git")
    database.store_credential(run.workspace_id, GITHUB_TOKEN, STORED_TOKEN, cipher)
    environ = {"OPENROUTER_API_KEY": ENVIRONMENT_KEY}
    first = worker_with(environ)
    first.start(run)
    cloned = await first.cloned(run)
    first.clones.release(run.run_id)
    await first.step(run, cloned)
    again = intake.offered[1]
    assert again.token_for("https://github.com/acme/app.git") == STORED_TOKEN


async def test_stored_token_that_cannot_be_read_ends_the_analysis_at_the_clone(
    worker_with, database, started, cipher, another_cipher, intake
):
    database.store_credential(started.workspace_id, GITHUB_TOKEN, STORED_TOKEN, cipher)
    worker = worker_with({"OPENROUTER_API_KEY": ENVIRONMENT_KEY}, cipher=another_cipher)
    failure = await refused(worker.cloned(started))
    assert (failure.type, failure.non_retryable) == (steps.ANALYSIS_FAILED, True)
    assert failure.message == (
        "the stored value of github_token cannot be read: set ENCRYPTION_KEY to the"
        " key it was stored with, or set the credential again"
    )
    assert failure.__cause__ is None
    assert intake.cloned == []
    assert not worker.clones.folder_of(started.run_id).exists()
