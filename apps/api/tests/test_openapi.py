"""Tests for the OpenAPI document, the contract the dashboard's types are generated from."""

from pathlib import Path

import pytest

import openmarketer_api.routes
from openmarketer_api.main import create_app
from openmarketer_api.openapi import SCHEMA_PATH, render_schema

OPERATIONS = {
    "getHealth": ("get", "/health"),
    "createProject": ("post", "/v1/projects"),
    "listProjects": ("get", "/v1/projects"),
    "getProject": ("get", "/v1/projects/{project_id}"),
    "startAnalysis": ("post", "/v1/projects/{project_id}/analyses"),
    "listAnalyses": ("get", "/v1/projects/{project_id}/analyses"),
    "getAnalysis": ("get", "/v1/projects/{project_id}/analyses/{run_id}"),
    "getDraftProfile": ("get", "/v1/projects/{project_id}/profile/draft"),
    "getApprovedProfile": ("get", "/v1/projects/{project_id}/profile/approved"),
    "listProfileVersions": ("get", "/v1/projects/{project_id}/profile/versions"),
    "getProfileVersion": ("get", "/v1/projects/{project_id}/profile/versions/{version}"),
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


LISTS = {
    "/v1/projects": ("ProjectListResponse", "projects"),
    "/v1/projects/{project_id}/analyses": ("AnalysisRunListResponse", "runs"),
    "/v1/projects/{project_id}/profile/versions": ("ProfileVersionListResponse", "versions"),
}


@pytest.mark.parametrize("path", LISTS)
def test_list_is_bounded_and_paged_the_same_way_everywhere(schema, path):
    query = {p["name"]: p for p in schema["paths"][path]["get"]["parameters"] if p["in"] == "query"}
    assert set(query) == {"limit", "cursor"}
    limit = query["limit"]["schema"]
    assert (limit["minimum"], limit["default"], limit["maximum"]) == (1, 50, 100)
    assert (query["cursor"]["required"], query["cursor"]["schema"]["type"]) == (False, "string")


@pytest.mark.parametrize(("path", "response"), LISTS.items())
def test_list_puts_its_items_under_a_named_key_beside_the_next_cursor(schema, path, response):
    model, items = response
    listing = schema["components"]["schemas"][model]
    assert set(listing["required"]) == set(listing["properties"]) == {items, "next_cursor"}
    assert listing["additionalProperties"] is False


def test_read_routes_take_no_request_body(schema):
    for path, methods in schema["paths"].items():
        assert "get" not in methods or "requestBody" not in methods["get"], path


def test_version_list_carries_no_profile(schema):
    summary = schema["components"]["schemas"]["ProfileVersionSummaryResponse"]
    full = schema["components"]["schemas"]["ProfileVersionResponse"]
    assert set(full["properties"]) - set(summary["properties"]) == {"profile"}


def test_profile_versions_expose_no_row_identifiers(schema):
    for name in ("ProfileVersionSummaryResponse", "ProfileVersionResponse"):
        fields = set(schema["components"]["schemas"][name]["properties"])
        assert not fields & {"id", "snapshot_id", "edited_from_id", "profile_id"}, name


def test_existing_project_of_a_conflict_is_an_optional_field_of_every_problem(schema):
    problem = schema["components"]["schemas"]["Problem"]
    assert problem["required"] == ["code", "message"]
    assert problem["properties"]["existing_project_id"]["format"] == "uuid"
    assert "anyOf" not in problem["properties"]["existing_project_id"]


def test_routes_import_no_database_or_agent_code():
    for module in Path(openmarketer_api.routes.__file__).parent.glob("*.py"):
        source = module.read_text()
        assert "sqlalchemy" not in source, module.name
        assert "openmarketer_core.analyzer" not in source, module.name
