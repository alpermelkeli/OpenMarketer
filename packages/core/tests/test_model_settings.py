"""Tests for a workspace's model settings: what resolves, where it comes from, and changing it.

They need PostgreSQL (see the root ``conftest.py``). The configuration is made up in the test.
"""

import uuid

import pytest
from sqlalchemy.orm import Session

from openmarketer_core.db.models import Workspace
from openmarketer_core.db.workspace_settings import store_setting, stored_settings
from openmarketer_core.llm_config import (
    CapabilityProblem,
    CatalogModel,
    Config,
    ModelProblem,
    ModelRouter,
    RoleConfig,
)
from openmarketer_core.workspace_settings.model_choices import ModelChoiceRefused, RoleChoice
from openmarketer_core.workspace_settings.model_settings import (
    ChoiceKind,
    ModelSetByEnvironment,
    RoleModelSource,
    StoredModelChoiceInvalid,
    TierModelSource,
    UnknownRole,
    UnknownTier,
    UnreadableChoice,
    model_choices,
    model_problems,
    model_settings,
    remove_role_choice,
    remove_tier_model,
    set_rejected_parameters,
    set_role_choice,
    set_tier_model,
)

OWNER = uuid.UUID("11111111-1111-4111-8111-111111111111")
CONFIG = Config(
    tiers={"fast": "file/fast", "balanced": "file/balanced", "strong": "file/strong"},
    defaults={"model": "file/default", "temperature": 0.4},  # type: ignore[arg-type]
    roles={
        "repo_analyzer": RoleConfig(tier="fast", temperature=0.1, requires=["tools"]),
        "writer": RoleConfig(model="file/writer"),
        "embeddings": RoleConfig(kind="embeddings", model="file/embed"),
    },
    unsupported_parameters={"file/writer": ["temperature"]},
)


def router(env=None) -> ModelRouter:
    return ModelRouter(CONFIG, env=env or {})


@pytest.fixture
def workspace_id(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


def settings_of(session, workspace_id, env=None):
    return model_settings(session, router(env), workspace_id=workspace_id)


def role_of(settings, name):
    return next(role for role in settings.roles if role.role == name)


def tier_of(settings, name):
    return next(tier for tier in settings.tiers if tier.tier == name)


def test_without_choices_everything_comes_from_the_file(session, workspace_id):
    settings = settings_of(session, workspace_id)
    assert [t.source for t in settings.tiers] == [TierModelSource.FILE] * 3
    analyzer = role_of(settings, "repo_analyzer")
    assert (analyzer.effective.model, analyzer.model_source) == ("file/fast", RoleModelSource.TIER)
    assert role_of(settings, "writer").model_source == RoleModelSource.FILE
    assert analyzer.choice is None and analyzer.changed is None


def test_tier_choice_is_followed_by_every_role_on_the_tier(session, workspace_id):
    set_tier_model(
        session, router(), workspace_id=workspace_id, tier="fast", model="ws/fast", set_by=OWNER
    )
    settings = settings_of(session, workspace_id)
    fast = tier_of(settings, "fast")
    assert (fast.model, fast.source, fast.chosen) == (
        "ws/fast",
        TierModelSource.WORKSPACE,
        "ws/fast",
    )
    assert fast.changed is not None and fast.changed.set_by == OWNER
    assert role_of(settings, "repo_analyzer").effective.model == "ws/fast"


def test_tier_choice_is_refused_while_the_environment_sets_the_tier(session, workspace_id):
    with pytest.raises(ModelSetByEnvironment, match="LLM_MODEL_FAST"):
        set_tier_model(
            session,
            router({"LLM_MODEL_FAST": "env/fast"}),
            workspace_id=workspace_id,
            tier="fast",
            model="ws/fast",
            set_by=OWNER,
        )


def test_unknown_tier_or_role_is_refused(session, workspace_id):
    with pytest.raises(UnknownTier):
        set_tier_model(
            session, router(), workspace_id=workspace_id, tier="huge", model="a/b", set_by=OWNER
        )
    with pytest.raises(UnknownRole):
        remove_role_choice(session, router(), workspace_id=workspace_id, role="poet")


def test_role_choice_overrides_its_parts_and_keeps_the_rest(session, workspace_id):
    choice = RoleChoice(model="ws/analyzer", temperature=0.6)
    set_role_choice(
        session,
        router(),
        workspace_id=workspace_id,
        role="repo_analyzer",
        choice=choice,
        set_by=OWNER,
    )
    analyzer = role_of(settings_of(session, workspace_id), "repo_analyzer")
    assert analyzer.effective.model == "ws/analyzer"
    assert analyzer.effective.temperature == 0.6
    assert analyzer.model_source == RoleModelSource.WORKSPACE
    assert analyzer.inherited.model == "file/fast"
    assert analyzer.inherited.temperature == 0.1
    assert analyzer.choice == choice


def test_role_model_is_refused_while_the_environment_sets_it_but_its_parameters_are_not(
    session, workspace_id
):
    env = {"LLM_MODEL__WRITER": "env/writer"}
    with pytest.raises(ModelSetByEnvironment):
        set_role_choice(
            session,
            router(env),
            workspace_id=workspace_id,
            role="writer",
            choice=RoleChoice(model="ws/writer"),
            set_by=OWNER,
        )
    set_role_choice(
        session,
        router(env),
        workspace_id=workspace_id,
        role="writer",
        choice=RoleChoice(max_tokens=99),
        set_by=OWNER,
    )
    writer = role_of(settings_of(session, workspace_id, env), "writer")
    assert (writer.effective.model, writer.effective.max_tokens) == ("env/writer", 99)
    assert writer.model_source == RoleModelSource.ENVIRONMENT


def test_empty_role_choice_removes_the_stored_one(session, workspace_id):
    set_role_choice(
        session,
        router(),
        workspace_id=workspace_id,
        role="writer",
        choice=RoleChoice(max_tokens=99),
        set_by=OWNER,
    )
    set_role_choice(
        session,
        router(),
        workspace_id=workspace_id,
        role="writer",
        choice=RoleChoice(),
        set_by=OWNER,
    )
    assert stored_settings(session, workspace_id=workspace_id, prefix="model.") == []


def test_removed_choices_fall_back_to_the_file(session, workspace_id):
    set_tier_model(
        session, router(), workspace_id=workspace_id, tier="fast", model="ws/fast", set_by=OWNER
    )
    set_role_choice(
        session,
        router(),
        workspace_id=workspace_id,
        role="writer",
        choice=RoleChoice(model="ws/writer"),
        set_by=OWNER,
    )
    remove_tier_model(session, router(), workspace_id=workspace_id, tier="fast")
    remove_role_choice(session, router(), workspace_id=workspace_id, role="writer")
    assert model_choices(session, router(), workspace_id=workspace_id).tiers == {}
    assert role_of(settings_of(session, workspace_id), "writer").effective.model == "file/writer"


def test_invalid_role_choice_is_refused_by_field(session, workspace_id):
    with pytest.raises(ModelChoiceRefused) as refusal:
        set_role_choice(
            session,
            router(),
            workspace_id=workspace_id,
            role="embeddings",
            choice=RoleChoice(temperature=0.1),
            set_by=OWNER,
        )
    assert refusal.value.field == "temperature"


def test_rejected_parameters_add_to_the_files_and_stop_temperature(session, workspace_id):
    set_tier_model(
        session, router(), workspace_id=workspace_id, tier="fast", model="ws/fast", set_by=OWNER
    )
    set_rejected_parameters(
        session,
        workspace_id=workspace_id,
        model="ws/fast",
        parameters=["temperature"],
        set_by=OWNER,
    )
    settings = settings_of(session, workspace_id)
    assert [(r.model, r.from_file, r.chosen) for r in settings.rejected_parameters] == [
        ("file/writer", {"temperature"}, frozenset()),
        ("ws/fast", frozenset(), {"temperature"}),
    ]
    assert not role_of(settings, "repo_analyzer").temperature_sent
    assert not role_of(settings, "writer").temperature_sent


def test_no_rejected_parameters_removes_the_mark(session, workspace_id):
    for parameters in (["temperature"], []):
        set_rejected_parameters(
            session,
            workspace_id=workspace_id,
            model="ws/fast",
            parameters=parameters,
            set_by=OWNER,
        )
    assert model_choices(session, router(), workspace_id=workspace_id).rejected_parameters == {}


def test_stored_choice_for_a_role_the_file_no_longer_has_is_ignored(session, workspace_id):
    store_setting(
        session,
        workspace_id=workspace_id,
        name="model.role.poet",
        value={"model": "a/b"},
        set_by=OWNER,
    )
    assert model_choices(session, router(), workspace_id=workspace_id).roles == {}


def test_stored_choice_that_breaks_a_rule_says_what_to_reset(session, workspace_id):
    store_setting(
        session,
        workspace_id=workspace_id,
        name="model.role.writer",
        value={"temperature": 7},
        set_by=OWNER,
    )
    with pytest.raises(StoredModelChoiceInvalid, match="the role writer"):
        model_choices(session, router(), workspace_id=workspace_id)


def test_stored_choice_that_breaks_a_rule_is_listed_so_it_can_be_reset(session, workspace_id):
    store_setting(
        session,
        workspace_id=workspace_id,
        name="model.role.writer",
        value={"temperature": 7},
        set_by=OWNER,
    )
    settings = settings_of(session, workspace_id)
    assert settings.unreadable == (UnreadableChoice(kind=ChoiceKind.ROLE, key="writer"),)
    assert role_of(settings, "writer").choice is None


def test_problems_are_reported_for_the_models_the_workspace_resolves_to(session, workspace_id):
    set_tier_model(
        session, router(), workspace_id=workspace_id, tier="fast", model="ws/fast", set_by=OWNER
    )
    catalog = {
        "ws/fast": CatalogModel(
            id="ws/fast",
            name="Fast",
            context_length=None,
            prompt_price=None,
            completion_price=None,
            supported_parameters=frozenset({"temperature"}),
            input_modalities=frozenset({"text"}),
        )
    }
    problems = model_problems(session, router(), catalog, workspace_id=workspace_id)
    assert problems["repo_analyzer"] == [ModelProblem("ws/fast", CapabilityProblem.NO_TOOLS)]
    assert problems["writer"] == [ModelProblem("file/writer", CapabilityProblem.NOT_IN_CATALOG)]


def test_decisions_role_sends_no_temperature_and_has_no_catalogue_problem(session, workspace_id):
    config = CONFIG.model_copy(
        update={
            "roles": {
                **CONFIG.roles,
                "screener": RoleConfig(kind="decisions", model="file/decide"),
            }
        }
    )
    with_decisions = ModelRouter(config, env={})

    screener = role_of(
        model_settings(session, with_decisions, workspace_id=workspace_id), "screener"
    )
    problems = model_problems(session, with_decisions, {}, workspace_id=workspace_id)

    assert screener.kind == "decisions" and not screener.temperature_sent
    assert screener.effective.fallbacks == []
    assert "screener" not in problems
