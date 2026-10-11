"""Tests for the framework-free half of the agent runtime: actions, steps and thread names.

``test_graph_thread.py`` covers the LangGraph half.
"""

import ast
import inspect
import subprocess
import sys
import uuid

import pytest
from pydantic import BaseModel, ConfigDict

from openmarketer_core import agent_runtime
from openmarketer_core.agent_runtime import actions, steps, threads
from openmarketer_core.agent_runtime.actions import (
    MAX_SHOWN_CHARACTERS,
    MAX_SHOWN_FIELDS,
    ActionCatalog,
    ActionDefinition,
    ActionOutcome,
    ActionRequest,
    InvalidActionDefinition,
    InvalidActionPayload,
    UnknownAction,
)
from openmarketer_core.agent_runtime.steps import StepOutcome
from openmarketer_core.agent_runtime.threads import thread_id
from openmarketer_core.db.analysis_runs import ANALYSIS_RUN_THREADS, run_thread_id

SECRET = "sk-live-4242-do-not-repeat"


class Recipient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str


class SendEmail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: list[Recipient]
    subject: str
    attempts: int = 1


class Publish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    post_id: str


CATALOG = ActionCatalog(
    [ActionDefinition("send_email", SendEmail), ActionDefinition("publish", Publish)]
)


def email(**changes) -> ActionRequest:
    payload = {"to": [{"address": "a@example.com"}], "subject": "Hello"} | changes
    return ActionRequest(action_id="analysis-run:r:i", name="send_email", payload=payload)


# ------------------------------------------------------------------ the catalog
def test_valid_request_is_returned_as_the_model_of_its_action():
    payload = CATALOG.validate(email(attempts=3))
    assert payload == SendEmail(
        to=[Recipient(address="a@example.com")], subject="Hello", attempts=3
    )


def test_catalog_names_every_action_it_allows():
    assert CATALOG.names == {"send_email", "publish"}


def test_action_that_is_not_in_the_catalog_is_refused():
    request = ActionRequest(action_id="t:i", name="delete_everything", payload={})
    with pytest.raises(UnknownAction, match="delete_everything") as refused:
        CATALOG.validate(request)
    assert refused.value.action_name == "delete_everything"


def test_name_of_an_unknown_action_is_shortened_in_the_message():
    request = ActionRequest(action_id="t:i", name="x" * 1000 + "\nforged log line", payload={})
    with pytest.raises(UnknownAction) as refused:
        CATALOG.validate(request)
    assert len(str(refused.value)) < 2 * MAX_SHOWN_CHARACTERS
    assert "\n" not in str(refused.value)


def test_field_that_the_model_does_not_have_is_refused():
    with pytest.raises(InvalidActionPayload, match="send_email") as refused:
        CATALOG.validate(email(cc="boss@example.com"))
    assert refused.value.fields == ("cc",)


def test_field_of_the_wrong_type_is_refused():
    with pytest.raises(InvalidActionPayload) as refused:
        CATALOG.validate(email(attempts="many"))
    assert refused.value.fields == ("attempts",)


def test_field_that_is_missing_is_refused():
    request = ActionRequest(action_id="t:i", name="publish", payload={})
    with pytest.raises(InvalidActionPayload) as refused:
        CATALOG.validate(request)
    assert refused.value.fields == ("post_id",)


def test_problem_inside_a_nested_value_names_the_path_to_it():
    with pytest.raises(InvalidActionPayload) as refused:
        CATALOG.validate(email(to=[{"address": "a@example.com"}, {"address": 7}]))
    assert refused.value.fields == ("to.1.address",)


@pytest.mark.parametrize(
    "changes",
    [
        {"attempts": SECRET},
        {"token": SECRET},
        {"to": [{"address": "a@example.com", "key": SECRET}]},
    ],
    ids=["wrong type", "extra field", "extra nested field"],
)
def test_refusal_names_the_field_and_never_repeats_what_was_sent(changes):
    with pytest.raises(InvalidActionPayload) as refused:
        CATALOG.validate(email(**changes))
    assert SECRET not in str(refused.value)
    assert SECRET not in repr(refused.value.fields)


def test_refusal_does_not_carry_the_librarys_error_that_holds_the_values():
    with pytest.raises(InvalidActionPayload) as refused:
        CATALOG.validate(email(attempts=SECRET))
    assert refused.value.__cause__ is None
    assert refused.value.__suppress_context__


def test_message_lists_only_the_first_fields_when_there_are_many():
    extra = {f"field_{n}": SECRET for n in range(100)}
    with pytest.raises(InvalidActionPayload) as refused:
        CATALOG.validate(email(**extra))
    assert len(refused.value.fields) == 100
    assert f"and {100 - MAX_SHOWN_FIELDS} more" in str(refused.value)
    assert "field_99" not in str(refused.value)


def test_two_definitions_of_one_action_are_refused():
    with pytest.raises(InvalidActionDefinition, match="send_email"):
        ActionCatalog(
            [ActionDefinition("send_email", SendEmail), ActionDefinition("send_email", Publish)]
        )


@pytest.mark.parametrize("extra", [None, "ignore", "allow"])
def test_payload_model_that_allows_extra_fields_is_refused(extra):
    config = ConfigDict() if extra is None else ConfigDict(extra=extra)
    lenient = type("Lenient", (BaseModel,), {"model_config": config, "__annotations__": {}})
    with pytest.raises(InvalidActionDefinition, match="forbid extra fields"):
        ActionDefinition("lenient", lenient)


def test_payload_model_that_forbids_extra_fields_through_a_parent_is_accepted():
    class Child(Publish):
        pass

    assert ActionDefinition("child", Child).payload is Child


# ----------------------------------------------------------------- the outcome
def test_outcome_that_succeeded_has_no_error():
    with pytest.raises(ValueError, match="no error"):
        ActionOutcome(action_id="t:i", succeeded=True, result={}, error="it broke")


def test_outcome_that_failed_says_what_went_wrong():
    with pytest.raises(ValueError, match="what went wrong"):
        ActionOutcome(action_id="t:i", succeeded=False, result={}, error="")


# ------------------------------------------------------------------- the steps
def test_step_that_finished_has_an_output_and_no_action():
    outcome = StepOutcome.finished({"profile": None})
    assert (outcome.output, outcome.action) == ({"profile": None}, None)


def test_step_that_finished_with_an_empty_output_is_still_finished():
    assert StepOutcome.finished({}).output == {}


def test_step_that_waits_has_an_action_and_no_output():
    request = email()
    outcome = StepOutcome.waiting_for(request)
    assert (outcome.output, outcome.action) == (None, request)


def test_step_with_both_an_output_and_an_action_is_refused():
    with pytest.raises(ValueError, match="either"):
        StepOutcome(output={}, action=email())


def test_step_with_neither_an_output_nor_an_action_is_refused():
    with pytest.raises(ValueError, match="either"):
        StepOutcome(output=None, action=None)


# ---------------------------------------------- what a workflow decodes is typed
@pytest.mark.parametrize(
    "decoded",
    [ActionRequest, ActionOutcome, StepOutcome, steps.GraphRunRef],
    ids=lambda c: c.__name__,
)
def test_type_a_workflow_decodes_has_annotations_that_are_types_not_text(decoded):
    # The workflow sandbox's data converter evaluates text annotations in the namespace of the
    # class's module, which may not be the module the sandbox has loaded: the run then hangs.
    assert not any(isinstance(annotation, str) for annotation in decoded.__annotations__.values())


# ---------------------------------------------------------------- thread names
RUN_ID = uuid.UUID("0b7e4c1a-5f3d-4e8a-9c21-6d1f8a7b2e90")


def test_thread_of_an_analysis_run_is_the_name_the_checkpoints_are_already_stored_under():
    assert thread_id("analysis-run", RUN_ID) == "analysis-run:0b7e4c1a-5f3d-4e8a-9c21-6d1f8a7b2e90"


def test_thread_of_an_analysis_run_is_the_one_the_cleanup_looks_for():
    assert thread_id("analysis-run", RUN_ID) == run_thread_id(RUN_ID)
    assert thread_id("analysis-run", RUN_ID).startswith(ANALYSIS_RUN_THREADS)


def test_thread_of_a_kind_with_digits_and_hyphens():
    assert thread_id("post-writer-2", RUN_ID) == f"post-writer-2:{RUN_ID}"


@pytest.mark.parametrize(
    "kind",
    ["", "Analysis", "analysis_run", "analysis run", "analysis:run", "100%", "run\n", "análisis"],
)
def test_kind_with_anything_but_lower_case_letters_digits_and_hyphens_is_refused(kind):
    with pytest.raises(ValueError, match="kind of run"):
        thread_id(kind, RUN_ID)


# ------------------------------------------------- no framework in the contract
FRAMEWORK_FREE = (agent_runtime, actions, steps, threads)
FRAMEWORKS = (
    "langgraph",
    "langchain",
    "temporalio",
    "sqlalchemy",
    "psycopg",
    # These load LangGraph and the database driver.
    "openmarketer_core.graph_checkpoints",
    "openmarketer_core.db",
)


def imported_names(module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", FRAMEWORK_FREE, ids=lambda module: module.__name__)
def test_contract_module_imports_no_framework(module):
    for name in imported_names(module):
        assert not any(name == f or name.startswith(f"{f}.") for f in FRAMEWORKS), name


def test_package_init_imports_nothing():
    assert imported_names(agent_runtime) == set()


def test_loading_the_contract_loads_no_framework():
    # A fresh interpreter: this one has loaded LangGraph for other tests.
    code = (
        "import sys\n"
        "import openmarketer_core.agent_runtime.actions\n"
        "import openmarketer_core.agent_runtime.steps\n"
        "import openmarketer_core.agent_runtime.threads\n"
        "frameworks = ('langgraph', 'langchain_core', 'temporalio', 'sqlalchemy', 'psycopg')\n"
        "loaded = [name for name in frameworks if name in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True)
