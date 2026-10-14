"""Tests for llm_config.py. Run: pytest config/test_llm_config.py"""
from pathlib import Path

import pytest

from llm_config import ConfigError, ModelRouter
from openmarketer_core.llm_config import CapabilityProblem, CatalogModel, ModelProblem

YAML = Path(__file__).with_name("models.yaml")
BASE_ENV = {"OPENROUTER_API_KEY": "test-key"}


def router(env=None, **kw):
    return ModelRouter.from_file(YAML, env={**BASE_ENV, **(env or {})}, **kw)


def test_yaml_loads_and_every_role_resolves():
    r = router()
    resolved = r.resolve_all()
    assert {"orchestrator", "reader", "repo_analyzer", "writer", "planner", "embeddings"} <= set(resolved)
    assert all(x.model for x in resolved.values())


def test_tier_is_used_when_role_has_no_explicit_model():
    r = router().resolve("writer")
    assert r.source == "tier:balanced"
    assert r.model == router().config.tiers["balanced"]


def test_tier_env_override_changes_every_role_in_the_tier():
    r = router({"LLM_MODEL_BALANCED": "openai/test-model"})
    assert r.resolve("writer").model == "openai/test-model"
    assert r.resolve("analyst").model == "openai/test-model"
    assert r.resolve("writer").source == "env-tier:balanced"
    assert r.resolve("reader").model != "openai/test-model"  # fast tier is untouched


def test_precedence_env_over_project_over_workspace_over_yaml():
    ws = {"writer": {"model": "google/ws-model"}}
    pr = {"writer": {"model": "google/pr-model"}}
    assert router(workspace_overrides=ws).resolve("writer").source == "workspace"
    assert router(workspace_overrides=ws, project_overrides=pr).resolve("writer").model == "google/pr-model"
    r = router({"LLM_MODEL__WRITER": "mistralai/env-model"}, workspace_overrides=ws, project_overrides=pr)
    assert r.resolve("writer").model == "mistralai/env-model"
    assert r.resolve("writer").source == "env"


def test_parameter_overrides_apply_per_role():
    r = router(project_overrides={"writer": {"temperature": 1.1, "max_tokens": 900}}).resolve("writer")
    assert (r.temperature, r.max_tokens) == (1.1, 900)
    assert router().resolve("writer").temperature == 0.8  # role value from yaml


def test_fallbacks_never_include_the_primary_model():
    r = router({"LLM_MODEL__READER": "anthropic/claude-haiku-4.5"}).resolve("reader")
    assert "anthropic/claude-haiku-4.5" not in r.fallbacks


def test_invalid_slug_and_unknown_role_are_rejected():
    with pytest.raises(ConfigError):
        router({"LLM_MODEL__WRITER": "not-a-slug"}).resolve("writer")
    with pytest.raises(ConfigError):
        router(project_overrides={"nope": {"model": "a/b"}})
    with pytest.raises(ConfigError):
        router(project_overrides={"writer": {"api_key": "x"}})
    with pytest.raises(KeyError):
        router().resolve("nope")


def test_reader_role_cannot_receive_tools():
    r = router()
    msgs = [{"role": "user", "content": "classify this"}]
    with pytest.raises(ConfigError):
        r.build_chat_request("reader", msgs, tools=[{"type": "function", "function": {"name": "x"}}])
    # roles that declare 'tools' may use them
    payload = r.build_chat_request("orchestrator", msgs, tools=[{"type": "function", "function": {"name": "get_status"}}])
    assert payload["tools"]


def test_request_payload_shape_for_openrouter():
    r = router()
    p = r.build_chat_request("writer", [{"role": "user", "content": "hi"}])
    assert p["model"] == r.resolve("writer").model
    assert p["models"][0] == p["model"] and len(p["models"]) >= 2
    assert p["provider"] == {"allow_fallbacks": True, "require_parameters": True, "data_collection": "deny"}
    h = r.headers()
    assert h["Authorization"] == "Bearer test-key" and h["X-Title"] == "OpenMarketer"


def test_temperature_is_left_out_for_a_model_that_rejects_it():
    msgs = [{"role": "user", "content": "hi"}]
    rejecting = router({"LLM_MODEL__WRITER": "anthropic/claude-sonnet-5.5"})
    assert "temperature" not in rejecting.build_chat_request("writer", msgs)
    accepting = router({"LLM_MODEL__WRITER": "anthropic/claude-haiku-4.5"})
    assert accepting.build_chat_request("writer", msgs)["temperature"] == 0.8


def test_missing_api_key_is_a_clear_error():
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        ModelRouter.from_file(YAML, env={}).headers()


def test_embeddings_role_is_not_a_chat_role():
    with pytest.raises(ConfigError):
        router().build_chat_request("embeddings", [])


def catalog_entry(slug, parameters, modalities=("text",)):
    return CatalogModel(
        id=slug,
        name=slug,
        context_length=None,
        prompt_price=None,
        completion_price=None,
        supported_parameters=frozenset(parameters),
        input_modalities=frozenset(modalities),
    )


CATALOG = {
    "anthropic/claude-sonnet-5.5": catalog_entry(
        "anthropic/claude-sonnet-5.5", ["tools", "structured_outputs"], ["text", "image"]
    ),
    "anthropic/claude-haiku-4.5": catalog_entry("anthropic/claude-haiku-4.5", ["temperature"]),
}


def only(r, role, model):
    return r.resolve(role).model_copy(update={"model": model, "fallbacks": []})


def test_capability_check_reports_missing_features():
    r = router()
    assert r.check_capabilities(only(r, "orchestrator", "anthropic/claude-sonnet-5.5"), CATALOG) == []
    bad = r.check_capabilities(only(r, "vision", "anthropic/claude-haiku-4.5"), CATALOG)
    assert ModelProblem("anthropic/claude-haiku-4.5", CapabilityProblem.NO_IMAGE_INPUT) in bad
    unknown = r.check_capabilities(only(r, "writer", "x/y"), CATALOG)
    assert unknown == [ModelProblem("x/y", CapabilityProblem.NOT_IN_CATALOG)]


def test_capability_check_reports_temperature_only_when_the_request_carries_it():
    catalog = {"openai/o9": catalog_entry("openai/o9", [])}
    sent = router().check_capabilities(only(router(), "writer", "openai/o9"), catalog)
    assert sent == [ModelProblem("openai/o9", CapabilityProblem.REJECTS_TEMPERATURE)]
    marked = router(workspace_rejected_parameters={"openai/o9": ["temperature"]})
    assert marked.check_capabilities(only(marked, "writer", "openai/o9"), catalog) == []


def test_workspace_tier_is_used_unless_the_environment_sets_the_tier():
    chosen = router(workspace_tiers={"balanced": "openai/ws-tier"})
    assert chosen.resolve("writer").model == "openai/ws-tier"
    assert chosen.resolve("writer").source == "workspace-tier:balanced"
    from_env = router({"LLM_MODEL_BALANCED": "openai/env-tier"}, workspace_tiers={"balanced": "openai/ws-tier"})
    assert from_env.resolve("writer").model == "openai/env-tier"


def test_workspace_role_choice_wins_over_the_workspace_tier():
    r = router(
        workspace_overrides={"writer": {"model": "google/ws-role"}},
        workspace_tiers={"balanced": "openai/ws-tier"},
    )
    assert r.resolve("writer").model == "google/ws-role"


def test_temperature_is_left_out_for_a_model_the_workspace_marked():
    msgs = [{"role": "user", "content": "hi"}]
    r = router(
        {"LLM_MODEL__WRITER": "anthropic/claude-haiku-4.5"},
        workspace_rejected_parameters={"anthropic/claude-haiku-4.5": ["temperature"]},
    )
    assert "temperature" not in r.build_chat_request("writer", msgs)


def test_workspace_choices_for_unknown_tiers_or_parameters_are_rejected():
    with pytest.raises(ConfigError):
        router(workspace_tiers={"huge": "a/b"})
    with pytest.raises(ConfigError):
        router(workspace_rejected_parameters={"a/b": ["top_p"]})


def test_with_workspace_keeps_the_environment_and_replaces_the_workspace_layers():
    base = router({"LLM_MODEL__READER": "openai/env-reader"})
    r = base.with_workspace(
        overrides={"writer": {"model": "google/ws-role"}}, tiers={}, rejected_parameters={}
    )
    assert r.resolve("reader").model == "openai/env-reader"
    assert r.resolve("writer").model == "google/ws-role"
    assert base.resolve("writer").source == "tier:balanced"
