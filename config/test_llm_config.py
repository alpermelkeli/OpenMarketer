"""Tests for llm_config.py. Run: pytest config/test_llm_config.py"""
from pathlib import Path

import pytest

from llm_config import ConfigError, ModelRouter

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


def test_missing_api_key_is_a_clear_error():
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        ModelRouter.from_file(YAML, env={}).headers()


def test_embeddings_role_is_not_a_chat_role():
    with pytest.raises(ConfigError):
        router().build_chat_request("embeddings", [])


def test_capability_check_reports_missing_features():
    r = router()
    catalog = {
        "anthropic/claude-sonnet-5.5": {"supported_parameters": ["tools", "structured_outputs"], "architecture": {"input_modalities": ["text", "image"]}},
        "anthropic/claude-haiku-4.5": {"supported_parameters": ["temperature"], "architecture": {"input_modalities": ["text"]}},
    }
    ok = r.check_capabilities(r.resolve("orchestrator").model_copy(update={"model": "anthropic/claude-sonnet-5.5", "fallbacks": []}), catalog)
    assert ok == []
    bad = r.check_capabilities(r.resolve("vision").model_copy(update={"model": "anthropic/claude-haiku-4.5", "fallbacks": []}), catalog)
    assert any("image" in p for p in bad)
    unknown = r.check_capabilities(r.resolve("writer").model_copy(update={"model": "x/y", "fallbacks": []}), catalog)
    assert any("not found" in p for p in unknown)
