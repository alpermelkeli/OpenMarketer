"""Tests for the OpenAPI document, the contract the dashboard's types are generated from."""

from pathlib import Path

import pytest

import openmarketer_api.routes
from openmarketer_api.main import create_app
from openmarketer_api.openapi import SCHEMA_PATH, render_schema

OPERATIONS = {
    "getHealth": ("get", "/health"),
    "createProject": ("post", "/v1/projects"),
    "startAnalysis": ("post", "/v1/projects/{project_id}/analyses"),
    "getAnalysis": ("get", "/v1/projects/{project_id}/analyses/{run_id}"),
    "getDraftProfile": ("get", "/v1/projects/{project_id}/profile/draft"),
    "getApprovedProfile": ("get", "/v1/projects/{project_id}/profile/approved"),
    "saveProfileEdit": ("post", "/v1/projects/{project_id}/profile/versions/{version}/edits"),
    "approveProfileVersion": (
        "post",
        "/v1/projects/{project_id}/profile/versions/{version}/approval",
    ),
}


@pytest.fixture(scope="module")
def schema() -> dict:
    return create_app().openapi()


def operations(schema: dict) -> dict[str, tuple[str, str]]:
    return {
        operation["operationId"]: (method, path)
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
    }


def test_committed_schema_matches_the_routes():
    assert SCHEMA_PATH.read_text() == render_schema(), "run `make openapi` and commit the result"


def test_operation_ids_are_the_published_ones(schema):
    assert operations(schema) == OPERATIONS


def test_every_workspace_route_declares_its_refusal_and_database_errors(schema):
    problem = {"$ref": "#/components/schemas/Problem"}
    for path, methods in schema["paths"].items():
        if not path.startswith("/v1/"):
            continue
        for operation in methods.values():
            for status in ("403", "503"):
                declared = operation["responses"][status]["content"]["application/json"]["schema"]
                assert declared == problem, (path, status)


def test_profile_has_one_schema_for_requests_and_responses(schema):
    names = [name for name in schema["components"]["schemas"] if name.startswith("ProductProfile")]
    assert names == ["ProductProfile"]


def test_approval_takes_no_request_body(schema):
    path = "/v1/projects/{project_id}/profile/versions/{version}/approval"
    approval = schema["paths"][path]["post"]
    assert "requestBody" not in approval
    assert [p["name"] for p in approval["parameters"]] == ["project_id", "version"]


def test_routes_import_no_database_or_agent_code():
    for module in Path(openmarketer_api.routes.__file__).parent.glob("*.py"):
        source = module.read_text()
        assert "sqlalchemy" not in source, module.name
        assert "openmarketer_core.analyzer" not in source, module.name
