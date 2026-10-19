"""Tests for the routes of the budget for generated media. They need PostgreSQL."""

import pytest

from openmarketer_api.dependencies import db_session, environment
from openmarketer_api.identity import LOCAL_USER_ID

URL = "/v1/settings/creative-budget"


@pytest.fixture
def environ() -> dict[str, str]:
    return {}


@pytest.fixture(autouse=True)
def database(app, session, environ) -> None:
    app.dependency_overrides[db_session] = lambda: session
    app.dependency_overrides[environment] = lambda: environ


def test_without_a_choice_the_defaults_apply_with_their_source(client):
    body = client.get(URL).json()
    assert body["monthly"] == {
        "value_usd": 60.0,
        "source": "default",
        "default_usd": 60.0,
        "environment_variable": "MEDIA_MONTHLY_CAP_USD",
    }
    assert (body["auto_per_action"]["value_usd"], body["auto_per_action"]["source"]) == (
        0.25,
        "default",
    )
    assert (body["chosen"], body["set_by"], body["unreadable"]) == (None, None, False)
    assert body["bounds"] == {"max_monthly_usd": 10000.0, "max_auto_per_action_usd": 100.0}


def test_this_months_spending_is_shown_against_the_cap(client):
    month = client.get(URL).json()["spent_this_month"]
    assert (month["spent_usd"], month["reserved_usd"]) == (0.0, 0.0)
    assert month["cap_usd"] == month["remaining_usd"] == 60.0
    assert month["month_start"].endswith(("Z", "+00:00"))


def test_a_cap_the_environment_sets_shows_that_source(client, environ):
    environ["MEDIA_MONTHLY_CAP_USD"] = "12.5"
    body = client.get(URL).json()
    assert (body["monthly"]["value_usd"], body["monthly"]["source"]) == (12.5, "environment")
    assert body["spent_this_month"]["cap_usd"] == 12.5


def test_a_chosen_cap_applies_and_the_other_keeps_its_default(client):
    response = client.put(URL, json={"monthly_usd": 20})
    assert response.status_code == 200
    body = response.json()
    assert (body["monthly"]["value_usd"], body["monthly"]["source"]) == (20.0, "stored")
    assert body["auto_per_action"]["source"] == "default"
    assert body["chosen"] == {"monthly_usd": 20.0, "auto_per_action_usd": None}
    assert body["set_by"] == str(LOCAL_USER_ID)


def test_choosing_a_cap_the_environment_sets_is_refused(client, environ):
    environ["MEDIA_AUTO_APPROVE_USD"] = "1"
    response = client.put(URL, json={"auto_per_action_usd": 2})
    assert (response.status_code, response.json()["code"]) == (409, "cap_set_by_environment")


@pytest.mark.parametrize(
    ("body", "field"),
    [({"monthly_usd": 20000}, "monthly_usd"), ({"auto_per_action_usd": -1}, "auto_per_action_usd")],
)
def test_a_cap_out_of_bounds_is_refused_beside_its_field(client, body, field):
    response = client.put(URL, json=body)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]


def test_an_unknown_cap_is_refused(client):
    assert client.put(URL, json={"daily_usd": 1}).status_code == 422


def test_removing_the_choice_brings_back_the_defaults(client):
    client.put(URL, json={"monthly_usd": 5})
    assert client.delete(URL).status_code == 204
    assert client.get(URL).json()["monthly"]["source"] == "default"


def test_removing_takes_no_body(client):
    assert client.request("DELETE", URL, json={"monthly_usd": 1}).status_code == 422
