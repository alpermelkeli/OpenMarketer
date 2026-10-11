"""Tests for the routes of an agent's actions: their names, catalog and policies."""

import pytest
from scripted_work import Publish, ScriptedAction, SendEmail, route

from openmarketer_core.agent_runtime.actions import ActionRequest, InvalidActionDefinition
from openmarketer_worker.agent_runtime.routes import ActionContext, ActionRoutes
from openmarketer_worker.agent_runtime.vocabulary import ActionPolicy

QUICK = ActionPolicy(attempt_timeout_seconds=5, max_attempts=2, retry_after_seconds=0.1)
LONG = ActionPolicy(
    attempt_timeout_seconds=600, max_attempts=3, retry_after_seconds=5, heartbeat_timeout_seconds=30
)


def test_two_routes_of_one_name_are_refused():
    with pytest.raises(InvalidActionDefinition, match="'send_email' is defined twice"):
        ActionRoutes(
            [
                route("send_email", SendEmail, ScriptedAction(), QUICK),
                route("send_email", Publish, ScriptedAction(), LONG),
            ]
        )


def test_policies_are_the_names_of_the_actions_with_their_own_limits():
    routes = ActionRoutes(
        [
            route("send_email", SendEmail, ScriptedAction(), QUICK),
            route("publish", Publish, ScriptedAction(), LONG),
        ]
    )
    assert routes.policies == {"send_email": QUICK, "publish": LONG}


def test_catalog_allows_the_names_of_the_routes_and_no_other():
    routes = ActionRoutes([route("send_email", SendEmail, ScriptedAction(), QUICK)])
    assert routes.catalog.names == {"send_email"}


def test_catalog_checks_the_payload_against_the_model_of_the_route():
    routes = ActionRoutes([route("publish", Publish, ScriptedAction(), QUICK)])
    payload = routes.catalog.validate(ActionRequest("run:1:2", "publish", {"post": 7}))
    assert payload == Publish(post=7)


def test_route_of_a_name_holds_the_handler_that_runs_it():
    handler = ScriptedAction([{"url": "https://example.com"}])
    routes = ActionRoutes([route("publish", Publish, handler, QUICK)])
    held = routes.route("publish")
    assert held.handler(Publish(post=7), ActionContext("run:1:2")) == {"url": "https://example.com"}
    assert held.policy is QUICK


def test_agent_with_no_actions_has_no_routes():
    routes = ActionRoutes([])
    assert (routes.catalog.names, routes.policies) == (frozenset(), {})
