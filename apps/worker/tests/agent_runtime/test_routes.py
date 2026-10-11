"""Tests for the routes of an agent's actions: their names, the catalog and the handlers."""

import pytest
from scripted_actions import Publish, ScriptedAction, SendEmail, route

from openmarketer_core.agent_runtime.actions import (
    ActionRequest,
    InvalidActionDefinition,
    UnknownAction,
)
from openmarketer_worker.agent_runtime.routes import ActionContext, ActionRoutes


def test_two_routes_of_one_name_are_refused():
    with pytest.raises(InvalidActionDefinition, match="'send_email' is defined twice"):
        ActionRoutes(
            [
                route("send_email", SendEmail, ScriptedAction()),
                route("send_email", Publish, ScriptedAction()),
            ]
        )


def test_catalog_refuses_a_name_that_is_not_one_of_the_routes():
    routes = ActionRoutes([route("send_email", SendEmail, ScriptedAction())])
    with pytest.raises(UnknownAction):
        routes.catalog.validate(ActionRequest("run:1:2", "publish", {"post": 7}))


def test_catalog_checks_the_payload_against_the_model_of_the_route():
    routes = ActionRoutes([route("publish", Publish, ScriptedAction())])
    payload = routes.catalog.validate(ActionRequest("run:1:2", "publish", {"post": 7}))
    assert payload == Publish(post=7)


def test_route_of_a_name_holds_the_handler_that_runs_it():
    handler = ScriptedAction([{"url": "https://example.com"}])
    routes = ActionRoutes([route("publish", Publish, handler)])
    held = routes.route("publish")
    assert held.handler(Publish(post=7), ActionContext("run:1:2")) == {"url": "https://example.com"}


def test_agent_with_no_actions_allows_none():
    routes = ActionRoutes([])
    with pytest.raises(UnknownAction):
        routes.catalog.validate(ActionRequest("run:1:2", "publish", {"post": 7}))
