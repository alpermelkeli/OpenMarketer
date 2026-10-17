"""Fixtures for the API tests: an application without services and a local client.

Tests put fakes or the test database in place by overriding dependencies. The
client looks like a request made on the machine the API runs on, which is what
single-user local mode requires.
"""

import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from openmarketer_api.dependencies import db_session
from openmarketer_api.identity import current_workspace_id
from openmarketer_api.main import create_app
from openmarketer_core.llm_catalog import ModelCatalog
from openmarketer_core.llm_config import ModelRouter
from openmarketer_core.research.page_store import PageStoreUnavailable

MODELS_FILE = Path(__file__).resolve().parents[3] / "config" / "models.yaml"


class UnreadableCatalog(ModelCatalog):
    """A provider's catalogue that cannot be read, so no test reaches the network."""

    def __init__(self) -> None:
        super().__init__("http://catalogue.invalid")

    def models(self):
        return None


class RecordingPageStore:
    """The object storage of these tests: what it removed, unless it is told to be unreachable."""

    def __init__(self) -> None:
        self.removed: list[str] = []
        self.unreachable = False

    def remove(self, key: str) -> None:
        if self.unreachable:
            raise PageStoreUnavailable("object storage is not reachable")
        self.removed.append(key)


@pytest.fixture
def page_store() -> RecordingPageStore:
    return RecordingPageStore()


@pytest.fixture
def repository_models() -> ModelRouter:
    """The repository's own model configuration, with an empty environment."""
    return ModelRouter.from_file(MODELS_FILE, env={})


@pytest.fixture
def no_catalog() -> ModelCatalog:
    return UnreadableCatalog()


@pytest.fixture
def app() -> FastAPI:
    return create_app()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000))


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.UUID("11111111-1111-4111-8111-111111111111")


@pytest.fixture
def without_database(app, workspace_id) -> None:
    """For routes tested against fakes: a fixed workspace and a session nothing uses."""
    app.dependency_overrides[db_session] = lambda: Session()
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
