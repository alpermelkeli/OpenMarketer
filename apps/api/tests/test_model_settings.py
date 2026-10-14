"""Tests for the model settings routes. They need PostgreSQL (see the root ``conftest.py``).

The configuration is made up here, so a test does not depend on the repository's choice of
models; the environment is empty unless a test sets a variable, and the provider's catalogue
is a fake.
"""

import pytest
from sqlalchemy.orm import Session

from openmarketer_api.dependencies import db_session, model_catalog, model_router
from openmarketer_api.identity import LOCAL_USER_ID
from openmarketer_core.db.evidence_store import local_workspace_id
from openmarketer_core.db.workspace_settings import store_setting
from openmarketer_core.llm_catalog import ModelCatalog
from openmarketer_core.llm_config import CatalogModel, Config, ModelRouter, RoleConfig

URL = "/v1/settings/models"
CONFIG = Config(
    tiers={"fast": "file/fast", "balanced": "file/balanced", "strong": "file/strong"},
    defaults={"model": "file/default", "temperature": 0.4, "max_tokens": 1000},  # type: ignore[arg-type]
    roles={
        "repo_analyzer": RoleConfig(tier="fast", temperature=0.1, requires=["tools"]),
        "embeddings": RoleConfig(kind="embeddings", model="file/embed"),
    },
    unsupported_parameters={"file/strong": ["temperature"]},
)


class FakeCatalog(ModelCatalog):
    def __init__(self, models: dict[str, CatalogModel] | None) -> None:
        super().__init__("http://catalogue.invalid")
        self.found = models

    def models(self):
        return self.found


def listed(model_id: str, *parameters: str) -> CatalogModel:
    return CatalogModel(
        id=model_id,
        name=model_id.title(),
        context_length=128000,
        prompt_price=1.0,
        completion_price=2.0,
        supported_parameters=frozenset(parameters),
        input_modalities=frozenset({"text"}),
    )


@pytest.fixture(autouse=True)
def database(app, session) -> None:
    app.dependency_overrides[db_session] = lambda: session


@pytest.fixture(autouse=True)
def environ(app) -> dict[str, str]:
    """The environment the configuration is read with; a test adds the variables it needs."""
    variables: dict[str, str] = {}
    app.dependency_overrides[model_router] = lambda: ModelRouter(CONFIG, env=variables)
    return variables


@pytest.fixture(autouse=True)
def catalog(app) -> FakeCatalog:
    fake = FakeCatalog({"file/fast": listed("file/fast", "tools", "temperature")})
    app.dependency_overrides[model_catalog] = lambda: fake
    return fake


def role_of(body: dict, name: str) -> dict:
    return next(role for role in body["roles"] if role["role"] == name)


def tier_of(body: dict, name: str) -> dict:
    return next(tier for tier in body["tiers"] if tier["tier"] == name)


# ------------------------------------------------------------------- read
def test_settings_list_every_tier_and_role_of_the_configuration(client):
    body = client.get(URL).json()
    assert [tier["tier"] for tier in body["tiers"]] == ["fast", "balanced", "strong"]
    assert [role["role"] for role in body["roles"]] == ["repo_analyzer", "embeddings"]
    assert tier_of(body, "fast") == {
        "tier": "fast",
        "model": "file/fast",
        "source": "file",
        "file_model": "file/fast",
        "environment_variable": "LLM_MODEL_FAST",
        "chosen": None,
        "changed": None,
    }


def test_role_says_what_it_resolves_to_and_where_its_model_comes_from(client):
    analyzer = role_of(client.get(URL).json(), "repo_analyzer")
    assert analyzer["effective"] == {
        "model": "file/fast",
        "fallbacks": [],
        "temperature": 0.1,
        "max_tokens": 1000,
        "reasoning_effort": None,
    }
    assert (analyzer["model_source"], analyzer["tier"]) == ("tier", "fast")
    assert analyzer["requires"] == ["tools"]
    assert analyzer["temperature_sent"] is True
    assert analyzer["choice"] is None
    assert analyzer["environment_variable"] == "LLM_MODEL__REPO_ANALYZER"


def test_model_the_environment_sets_is_shown_as_such(client, environ):
    environ["LLM_MODEL_FAST"] = "env/fast"
    body = client.get(URL).json()
    assert (tier_of(body, "fast")["model"], tier_of(body, "fast")["source"]) == (
        "env/fast",
        "environment",
    )


def test_bounds_of_a_choice_are_given(client):
    bounds = client.get(URL).json()["bounds"]
    assert bounds["rejectable_parameters"] == ["temperature"]
    assert bounds["max_fallbacks"] == 4


# ------------------------------------------------------------------ tiers
def test_choosing_a_tier_moves_every_role_on_it(client):
    response = client.put(f"{URL}/tiers/fast", json={"model": " openai/chosen "})
    assert response.status_code == 200
    body = response.json()
    fast = tier_of(body, "fast")
    assert (fast["model"], fast["source"], fast["chosen"]) == (
        "openai/chosen",
        "workspace",
        "openai/chosen",
    )
    assert fast["changed"]["set_by"] == str(LOCAL_USER_ID)
    assert role_of(body, "repo_analyzer")["effective"]["model"] == "openai/chosen"


def test_tier_the_environment_sets_cannot_be_chosen(client, environ):
    environ["LLM_MODEL_FAST"] = "env/fast"
    response = client.put(f"{URL}/tiers/fast", json={"model": "openai/chosen"})
    assert response.status_code == 409
    assert response.json()["code"] == "model_set_by_environment"
    assert "LLM_MODEL_FAST" in response.json()["message"]


def test_model_that_is_not_a_slug_is_refused_beside_its_field(client):
    response = client.put(f"{URL}/tiers/fast", json={"model": "gpt-9"})
    assert response.status_code == 422
    (refusal,) = response.json()["detail"]
    assert refusal["loc"] == ["body", "model"]


def test_unknown_tier_is_not_found(client):
    response = client.put(f"{URL}/tiers/huge", json={"model": "a/b"})
    assert (response.status_code, response.json()["code"]) == (404, "tier_not_found")


def test_removing_a_tier_choice_goes_back_to_the_file(client):
    client.put(f"{URL}/tiers/fast", json={"model": "openai/chosen"})
    assert client.delete(f"{URL}/tiers/fast").status_code == 204
    assert tier_of(client.get(URL).json(), "fast")["source"] == "file"


# ------------------------------------------------------------------ roles
def test_role_choice_keeps_the_files_values_for_what_it_leaves_out(client):
    response = client.put(
        f"{URL}/roles/repo_analyzer",
        json={"model": "openai/chosen", "fallbacks": ["openai/backup"], "reasoning_effort": "low"},
    )
    analyzer = role_of(response.json(), "repo_analyzer")
    assert analyzer["effective"] == {
        "model": "openai/chosen",
        "fallbacks": ["openai/backup"],
        "temperature": 0.1,
        "max_tokens": 1000,
        "reasoning_effort": "low",
    }
    assert analyzer["inherited"]["model"] == "file/fast"
    assert analyzer["choice"] == {
        "model": "openai/chosen",
        "fallbacks": ["openai/backup"],
        "temperature": None,
        "max_tokens": None,
        "reasoning_effort": "low",
    }


@pytest.mark.parametrize(
    ("body", "field"),
    [({"temperature": 3}, "temperature"), ({"fallbacks": ["a/b", "a/b"]}, "fallbacks")],
)
def test_role_choice_that_breaks_a_rule_is_refused_beside_its_field(client, body, field):
    response = client.put(f"{URL}/roles/repo_analyzer", json=body)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]


def test_embeddings_role_takes_a_model_only(client):
    response = client.put(f"{URL}/roles/embeddings", json={"temperature": 0.2})
    assert response.status_code == 422
    assert client.put(f"{URL}/roles/embeddings", json={"model": "a/b"}).status_code == 200


def test_role_choice_takes_no_unknown_part(client):
    response = client.put(f"{URL}/roles/repo_analyzer", json={"api_key": "x"})
    assert response.status_code == 422


def test_removing_a_role_choice_goes_back_to_the_file(client):
    client.put(f"{URL}/roles/repo_analyzer", json={"temperature": 0.9})
    assert client.delete(f"{URL}/roles/repo_analyzer").status_code == 204
    assert role_of(client.get(URL).json(), "repo_analyzer")["choice"] is None


def test_unknown_role_is_not_found(client):
    response = client.delete(f"{URL}/roles/poet")
    assert (response.status_code, response.json()["code"]) == (404, "role_not_found")


# ---------------------------------------------------- rejected parameters
def test_model_marked_as_rejecting_temperature_is_listed_and_sent_none(client):
    client.put(f"{URL}/tiers/fast", json={"model": "openai/chosen"})
    response = client.put(
        f"{URL}/rejected-parameters", json={"model": "openai/chosen", "parameters": ["temperature"]}
    )
    body = response.json()
    assert [(r["model"], r["from_file"], r["chosen"]) for r in body["rejected_parameters"]] == [
        ("file/strong", ["temperature"], []),
        ("openai/chosen", [], ["temperature"]),
    ]
    assert role_of(body, "repo_analyzer")["temperature_sent"] is False


def test_only_known_parameters_can_be_marked(client):
    response = client.put(
        f"{URL}/rejected-parameters", json={"model": "a/b", "parameters": ["top_p"]}
    )
    assert response.status_code == 422


# ----------------------------------------------------------- unreadable
def test_stored_choice_that_breaks_a_rule_is_listed_and_can_be_removed(client, session: Session):
    store_setting(
        session,
        workspace_id=local_workspace_id(session),
        name="model.role.repo_analyzer",
        value={"temperature": 9},
        set_by=LOCAL_USER_ID,
    )
    assert client.get(URL).json()["unreadable"] == [{"kind": "role", "key": "repo_analyzer"}]
    assert client.get(f"{URL}/check").status_code == 409
    client.delete(f"{URL}/roles/repo_analyzer")
    assert client.get(URL).json()["unreadable"] == []


# --------------------------------------------------------- the catalogue
def test_catalogue_lists_the_providers_models(client):
    body = client.get(f"{URL}/catalog").json()
    assert body == {
        "available": True,
        "models": [
            {
                "id": "file/fast",
                "name": "File/Fast",
                "context_length": 128000,
                "prompt_price": 1.0,
                "completion_price": 2.0,
                "supported_parameters": ["temperature", "tools"],
                "input_modalities": ["text"],
            }
        ],
    }


def test_catalogue_that_cannot_be_read_is_said_to_be_unavailable(client, catalog):
    catalog.found = None
    assert client.get(f"{URL}/catalog").json() == {"available": False, "models": []}
    assert client.get(f"{URL}/check").json() == {"available": False, "problems": []}


def test_check_reports_what_a_roles_model_lacks(client):
    client.put(f"{URL}/roles/repo_analyzer", json={"model": "openai/unlisted"})
    body = client.get(f"{URL}/check").json()
    assert body == {
        "available": True,
        "problems": [
            {"role": "repo_analyzer", "model": "openai/unlisted", "problem": "not_in_catalog"},
            {"role": "embeddings", "model": "file/embed", "problem": "not_in_catalog"},
        ],
    }
