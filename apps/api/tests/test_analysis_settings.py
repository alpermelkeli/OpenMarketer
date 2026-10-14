"""Tests for the routes of the limits of an analysis. They need PostgreSQL."""

import pytest

from openmarketer_api.dependencies import db_session
from openmarketer_api.identity import LOCAL_USER_ID
from openmarketer_core.repository_analysis.analyzer_agent import Limits

URL = "/v1/settings/analysis"
DEFAULTS = {"max_steps": Limits().max_steps, "max_cost_usd": Limits().max_cost_usd}


@pytest.fixture(autouse=True)
def database(app, session) -> None:
    app.dependency_overrides[db_session] = lambda: session


def test_without_a_choice_the_defaults_apply(client):
    body = client.get(URL).json()
    assert body["limits"] == body["defaults"] == DEFAULTS
    assert (body["chosen"], body["set_by"], body["unreadable"]) == (None, None, False)
    assert body["bounds"] == {
        "min_steps": 5,
        "max_steps": 200,
        "min_cost_usd": 0.01,
        "max_cost_usd": 20.0,
    }


def test_chosen_limit_applies_and_the_other_keeps_its_default(client):
    response = client.put(URL, json={"max_cost_usd": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["limits"] == {**DEFAULTS, "max_cost_usd": 2.0}
    assert body["chosen"] == {"max_steps": None, "max_cost_usd": 2.0}
    assert body["set_by"] == str(LOCAL_USER_ID)


@pytest.mark.parametrize(
    ("body", "field"), [({"max_steps": 1}, "max_steps"), ({"max_cost_usd": 999}, "max_cost_usd")]
)
def test_limit_out_of_bounds_is_refused_beside_its_field(client, body, field):
    response = client.put(URL, json=body)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]


def test_unknown_limit_is_refused(client):
    assert client.put(URL, json={"max_submissions": 9}).status_code == 422


def test_removing_the_choice_brings_back_the_defaults(client):
    client.put(URL, json={"max_steps": 80})
    assert client.delete(URL).status_code == 204
    assert client.get(URL).json()["limits"] == DEFAULTS
