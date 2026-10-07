"""OpenMarketer LLM model configuration and per-role model resolution.

Every LLM call site in the system is a *role* (writer, reader, planner, ...).
This module loads ``models.yaml`` and resolves the model for a role in this
order (first match wins):

    1. environment variable   LLM_MODEL__<ROLE>
    2. project override       (dashboard settings, stored in the database)
    3. workspace override     (dashboard settings, stored in the database)
    4. models.yaml            roles.<role>.model
    5. models.yaml            tiers.<tier>   (LLM_MODEL_<TIER> env overrides a tier)
    6. models.yaml            defaults.model

It also builds OpenRouter chat-completion requests. OpenRouter exposes an
OpenAI-compatible API, so the same payload also works with other compatible
endpoints by changing ``base_url``.

Usage:
    python config/llm_config.py show               # table of role -> model + source
    python config/llm_config.py show --config config/models.yaml

Dependencies: pyyaml, pydantic>=2.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_CONFIG_PATH = "config/models.yaml"  # relative to the repository root

Capability = Literal["tools", "structured_output", "vision"]
Tier = Literal["fast", "balanced", "strong"]

# OpenRouter slugs look like "provider/model" with an optional ":variant" suffix.
SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._:+/-]+$")
ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """Raised for invalid configuration or invalid overrides."""


def _check_slug(value: str, where: str) -> str:
    if not SLUG_RE.match(value):
        raise ConfigError(f"{where}: '{value}' is not a valid 'provider/model' slug")
    return value


def _interpolate(obj: Any, env: Mapping[str, str]) -> Any:
    """Expand ${VAR} and ${VAR:-default} in every string of a loaded YAML tree."""
    if isinstance(obj, str):
        return ENV_RE.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), obj)
    if isinstance(obj, list):
        return [_interpolate(x, env) for x in obj]
    if isinstance(obj, dict):
        return {k: _interpolate(v, env) for k, v in obj.items()}
    return obj


# ----------------------------------------------------------------- schema
class Routing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_fallbacks: bool = True
    require_parameters: bool = True
    data_collection: Literal["allow", "deny"] = "deny"


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = "openrouter"
    base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    app_url: str = ""
    app_name: str = "OpenMarketer"
    routing: Routing = Routing()


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    temperature: float = 0.4
    max_tokens: int = 4096
    timeout_s: int = 120
    fallbacks: list[str] = Field(default_factory=list)

    @field_validator("model")
    @classmethod
    def _slug(cls, v: str) -> str:
        return _check_slug(v, "defaults.model")

    @field_validator("fallbacks")
    @classmethod
    def _slugs(cls, v: list[str]) -> list[str]:
        return [_check_slug(x, "defaults.fallbacks") for x in v]


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_cost_per_call_usd: float = 1.5


class RoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["chat", "embeddings"] = "chat"
    model: str | None = None
    tier: Tier | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    fallbacks: list[str] | None = None
    requires: list[Capability] = Field(default_factory=list)
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    dimensions: int | None = None

    @field_validator("model")
    @classmethod
    def _slug(cls, v: str | None) -> str | None:
        return v if v is None else _check_slug(v, "role.model")

    @field_validator("fallbacks")
    @classmethod
    def _slugs(cls, v: list[str] | None) -> list[str] | None:
        return v if v is None else [_check_slug(x, "role.fallbacks") for x in v]


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    provider: Provider = Provider()
    tiers: dict[Tier, str]
    defaults: Defaults
    limits: Limits = Limits()
    roles: dict[str, RoleConfig]

    @field_validator("tiers")
    @classmethod
    def _tier_slugs(cls, v: dict[str, str]) -> dict[str, str]:
        return {k: _check_slug(s, f"tiers.{k}") for k, s in v.items()}


class Resolved(BaseModel):
    """The effective settings for one role after applying every override."""

    role: str
    kind: str
    model: str
    source: str
    fallbacks: list[str]
    temperature: float
    max_tokens: int
    timeout_s: int
    requires: list[str]
    reasoning_effort: str | None = None
    dimensions: int | None = None

    @property
    def tools_allowed(self) -> bool:
        """Tools are available only to roles that declare the 'tools' capability."""
        return "tools" in self.requires


# ----------------------------------------------------------------- router
class ModelRouter:
    """Resolve per-role model settings and build OpenRouter requests."""

    OVERRIDABLE = {"model", "temperature", "max_tokens", "fallbacks", "reasoning_effort"}

    def __init__(
        self,
        config: Config,
        env: Mapping[str, str] | None = None,
        workspace_overrides: dict[str, dict[str, Any]] | None = None,
        project_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.env = dict(os.environ if env is None else env)
        self.workspace = workspace_overrides or {}
        self.project = project_overrides or {}
        for scope, data in (("workspace", self.workspace), ("project", self.project)):
            for role, values in data.items():
                self._validate_override(scope, role, values)

    # -- loading -------------------------------------------------------
    @classmethod
    def from_file(cls, path: str | Path, **kwargs: Any) -> ModelRouter:
        env = dict(os.environ if kwargs.get("env") is None else kwargs["env"])
        kwargs["env"] = env
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(Config.model_validate(_interpolate(raw, env)), **kwargs)

    # -- validation ----------------------------------------------------
    def _validate_override(self, scope: str, role: str, values: dict[str, Any]) -> None:
        if role not in self.config.roles:
            raise ConfigError(f"{scope} override for unknown role '{role}'")
        unknown = set(values) - self.OVERRIDABLE
        if unknown:
            raise ConfigError(f"{scope} override for '{role}': unsupported keys {sorted(unknown)}")
        if "model" in values:
            _check_slug(values["model"], f"{scope} override for '{role}'")
        for fb in values.get("fallbacks", []) or []:
            _check_slug(fb, f"{scope} override for '{role}' fallbacks")

    # -- resolution ----------------------------------------------------
    def _tier_model(self, tier: str) -> tuple[str, str]:
        override = self.env.get(f"LLM_MODEL_{tier.upper()}", "").strip()
        if override:
            return _check_slug(override, f"LLM_MODEL_{tier.upper()}"), f"env-tier:{tier}"
        return self.config.tiers[tier], f"tier:{tier}"  # type: ignore[index]

    def resolve(self, role: str) -> Resolved:
        if role not in self.config.roles:
            raise KeyError(f"unknown role '{role}'; known roles: {sorted(self.config.roles)}")
        rc, d = self.config.roles[role], self.config.defaults

        # model
        env_model = self.env.get(f"LLM_MODEL__{role.upper()}", "").strip()
        if env_model:
            model, source = _check_slug(env_model, f"LLM_MODEL__{role.upper()}"), "env"
        elif "model" in self.project.get(role, {}):
            model, source = self.project[role]["model"], "project"
        elif "model" in self.workspace.get(role, {}):
            model, source = self.workspace[role]["model"], "workspace"
        elif rc.model:
            model, source = rc.model, "role"
        elif rc.tier:
            model, source = self._tier_model(rc.tier)
        else:
            model, source = d.model, "default"

        # other parameters: project > workspace > role > defaults
        def pick(key: str, default: Any) -> Any:
            for layer in (self.project.get(role, {}), self.workspace.get(role, {})):
                if layer.get(key) is not None:
                    return layer[key]
            value = getattr(rc, key, None)
            return default if value is None else value

        fallbacks = [m for m in pick("fallbacks", d.fallbacks) if m != model]
        return Resolved(
            role=role,
            kind=rc.kind,
            model=model,
            source=source,
            fallbacks=fallbacks,
            temperature=pick("temperature", d.temperature),
            max_tokens=pick("max_tokens", d.max_tokens),
            timeout_s=d.timeout_s,
            requires=list(rc.requires),
            reasoning_effort=pick("reasoning_effort", None),
            dimensions=rc.dimensions,
        )

    def resolve_all(self) -> dict[str, Resolved]:
        return {r: self.resolve(r) for r in self.config.roles}

    # -- capability check ---------------------------------------------
    @staticmethod
    def check_capabilities(
        resolved: Resolved, catalog: Mapping[str, Mapping[str, Any]]
    ) -> list[str]:
        """Return human-readable problems for the chosen model, given an OpenRouter-style catalog.

        ``catalog`` maps model id -> entry with ``supported_parameters`` (list) and
        ``architecture.input_modalities`` (list), as returned by GET /models.
        Unknown models are reported, not guessed.
        """
        problems: list[str] = []
        for slug in [resolved.model, *resolved.fallbacks]:
            entry = catalog.get(slug)
            if entry is None:
                problems.append(f"{slug}: not found in model catalog")
                continue
            params = set(entry.get("supported_parameters", []))
            modalities = set((entry.get("architecture") or {}).get("input_modalities", []))
            if "tools" in resolved.requires and "tools" not in params:
                problems.append(f"{slug}: does not support tool calling")
            if "structured_output" in resolved.requires and not params & {
                "structured_outputs",
                "response_format",
            }:
                problems.append(f"{slug}: does not support structured output")
            if "vision" in resolved.requires and "image" not in modalities:
                problems.append(f"{slug}: does not accept image input")
        return problems

    # -- requests ------------------------------------------------------
    def headers(self) -> dict[str, str]:
        p = self.config.provider
        key = self.env.get(p.api_key_env, "")
        if not key:
            raise ConfigError(f"environment variable {p.api_key_env} is not set")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if p.app_url:
            headers["HTTP-Referer"] = p.app_url
        if p.app_name:
            headers["X-Title"] = p.app_name
        return headers

    def build_chat_request(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        r = self.resolve(role)
        if r.kind != "chat":
            raise ConfigError(f"role '{role}' is of kind '{r.kind}', not 'chat'")
        if tools and not r.tools_allowed:
            raise ConfigError(f"role '{role}' is not allowed to use tools")
        routing = self.config.provider.routing
        payload: dict[str, Any] = {
            "model": r.model,
            "messages": messages,
            "temperature": r.temperature,
            "max_tokens": r.max_tokens,
            "provider": routing.model_dump(),
        }
        if r.fallbacks:
            payload["models"] = [r.model, *r.fallbacks]  # OpenRouter tries these in order
        if r.reasoning_effort:
            payload["reasoning"] = {"effort": r.reasoning_effort}
        if tools:
            payload["tools"] = tools
        if response_format:
            payload["response_format"] = response_format
        if extra:
            payload.update(extra)
        return payload

    def complete(self, role: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        """POST a chat completion.

        Reference implementation; not exercised against the live API in tests.
        """
        payload = self.build_chat_request(role, messages, **kwargs)
        req = urllib.request.Request(
            self.config.provider.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=self.headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.resolve(role).timeout_s) as resp:  # noqa: S310
            return json.load(resp)


def fetch_catalog(router: ModelRouter) -> dict[str, dict[str, Any]]:
    """Fetch the OpenRouter model catalog (id -> entry) for the dashboard's model picker."""
    req = urllib.request.Request(
        router.config.provider.base_url.rstrip("/") + "/models",
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        data = json.load(resp)
    return {m["id"]: m for m in data.get("data", [])}


# ----------------------------------------------------------------- CLI
def _show(router: ModelRouter) -> None:
    rows = [
        (r.role, r.model, r.source, ",".join(r.requires) or "-")
        for r in router.resolve_all().values()
    ]
    widths = [
        max(len(x[i]) for x in rows + [("role", "model", "source", "requires")]) for i in range(4)
    ]
    for row in [("role", "model", "source", "requires"), *rows]:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show", help="print the effective model for every role")
    show.add_argument(
        "--config",
        default=os.environ.get("LLM_CONFIG_PATH", DEFAULT_CONFIG_PATH),
    )
    args = parser.parse_args()
    if args.cmd == "show":
        _show(ModelRouter.from_file(args.config))


if __name__ == "__main__":
    main()
