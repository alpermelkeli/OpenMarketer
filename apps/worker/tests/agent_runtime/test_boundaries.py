"""Tests for what each module of the runtime may import: the layering, read from the source.

The worker never imports LangGraph: a graph is core's, behind ``AgentGraph``. And the workflow
sandbox loads some of these modules, so they must not pull in the database, a graph framework or
the code of an activity. Both are checked on the source with ``ast``, and on what a fresh
interpreter really loads.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

import openmarketer_worker.agent_runtime as runtime

SOURCE = Path(runtime.__file__).parent
MODULES = sorted(SOURCE.glob("*.py"))
WORKFLOW_MODULES = [
    "__init__",
    "vocabulary",
    "workflow_support",
    "approval_wait",
    "driver",
    "workflow_dsl",
]
DATABASE = ("sqlalchemy", "psycopg", "openmarketer_core.db", "openmarketer_core.graph_checkpoints")
ACTIVITY_CODE = (
    "temporalio.activity",
    "openmarketer_worker.agent_runtime.activities",
    "openmarketer_worker.agent_runtime.activity_support",
    "openmarketer_worker.agent_runtime.agent_worker",
    "openmarketer_worker.agent_runtime.proposal_activities",
    "openmarketer_worker.agent_runtime.routes",
)


def imported_by(path: Path) -> list[str]:
    """The dotted names a module imports: ``import a.b`` and ``from a.b import c`` give ``a.b``."""
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            # ``from temporalio import activity`` imports ``temporalio.activity``.
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def is_under(name: str, packages: tuple[str, ...]) -> bool:
    return any(name == package or name.startswith(f"{package}.") for package in packages)


def is_a_graph_framework(name: str) -> bool:
    """LangGraph, LangChain and what is named after them (``langchain_core``)."""
    return name.startswith(("langgraph", "langchain"))


def loaded_after_importing(*modules: str) -> set[str]:
    """The modules a fresh interpreter has loaded once it has imported ``modules``."""
    code = f"import sys, json; import {', '.join(modules)}; print(json.dumps(sorted(sys.modules)))"
    # ``-I``: nothing of this process's environment or working folder reaches the import.
    done = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    )
    return set(json.loads(done.stdout))


def test_the_runtime_has_the_modules_these_tests_know():
    assert {path.stem for path in MODULES} == {
        "__init__",
        "activities",
        "activity_support",
        "agent_worker",
        "approval_wait",
        "driver",
        "proposal_activities",
        "routes",
        "vocabulary",
        "workflow_dsl",
        "workflow_support",
    }


@pytest.mark.parametrize("path", MODULES, ids=lambda path: path.name)
def test_no_module_imports_a_graph_framework(path):
    assert [name for name in imported_by(path) if is_a_graph_framework(name)] == []


@pytest.mark.parametrize("stem", WORKFLOW_MODULES)
def test_module_the_sandbox_loads_imports_no_database_and_no_code_of_an_activity(stem):
    imported = imported_by(SOURCE / f"{stem}.py")
    assert [name for name in imported if is_under(name, DATABASE + ACTIVITY_CODE)] == []


def test_package_imports_nothing():
    assert imported_by(SOURCE / "__init__.py") == []


def test_vocabulary_imports_no_workflow_engine_at_all():
    assert [name for name in imported_by(SOURCE / "vocabulary.py") if "temporalio" in name] == []


def test_vocabulary_loads_no_database_graph_framework_or_workflow_engine():
    loaded = loaded_after_importing("openmarketer_worker.agent_runtime.vocabulary")
    forbidden = [
        name
        for name in loaded
        if is_under(name, DATABASE + ("temporalio",)) or is_a_graph_framework(name)
    ]
    assert forbidden == []


def test_package_loads_nothing_of_core_or_of_the_workflow_engine():
    loaded = loaded_after_importing("openmarketer_worker.agent_runtime")
    assert [name for name in loaded if is_under(name, ("openmarketer_core", "temporalio"))] == []


def test_modules_of_the_workflow_load_no_database_graph_framework_or_activity_code():
    loaded = loaded_after_importing(
        "openmarketer_worker.agent_runtime.driver",
        "openmarketer_worker.agent_runtime.workflow_dsl",
        "openmarketer_worker.agent_runtime.workflow_support",
    )
    forbidden = [
        name
        for name in loaded
        if is_under(name, DATABASE + ACTIVITY_CODE) or is_a_graph_framework(name)
    ]
    assert forbidden == []
