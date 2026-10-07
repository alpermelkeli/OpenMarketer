"""Extractors for Rust, Python and Go project manifests.

``CargoExtractor`` (``Cargo.toml``), ``PyprojectExtractor`` (``pyproject.toml``)
and ``GoModExtractor`` (``go.mod``) produce the same facts as the other
manifest extractors:

    manifest.name, manifest.version, manifest.description,
    manifest.homepage, manifest.keywords
    manifest.framework   a known web framework among the dependencies
    manifest.platform    cli when the project ships a command, web for a web framework
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator
from typing import Any

from pydantic import JsonValue

from openmarketer_core.extraction import Fact
from openmarketer_core.intake import RepoFiles

MAX_TEXT = 500

PYTHON_FRAMEWORKS = {
    "django": "django",
    "fastapi": "fastapi",
    "flask": "flask",
    "streamlit": "streamlit",
    "gradio": "gradio",
}
RUST_FRAMEWORKS = {
    "axum": "axum",
    "actix-web": "actix",
    "rocket": "rocket",
    "leptos": "leptos",
    "yew": "yew",
    "tauri": "tauri",
    "bevy": "bevy",
}
RUST_PLATFORMS = {"tauri": "desktop", "bevy": "desktop"}
REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
GO_MODULE_RE = re.compile(r"^\s*module\s+(\S+)")


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _table(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class _Toml:
    """A parsed TOML file that can say on which line a key is assigned."""

    def __init__(self, files: RepoFiles, path: str) -> None:
        self.path = path
        text = files.read_text(path)
        self.lines = text.splitlines()
        try:
            self.data: dict[str, Any] = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            self.data = {}

    def fact(self, kind: str, value: JsonValue, key: str) -> Fact:
        pattern = re.compile(rf"""^\s*["']?{re.escape(key)}["']?\s*=""")
        line = next((i for i, text in enumerate(self.lines, 1) if pattern.match(text)), None)
        return Fact(kind=kind, value=value, file=self.path, start_line=line, end_line=line)

    def text_facts(self, table: dict[str, Any], keys: dict[str, str]) -> Iterator[Fact]:
        for key, kind in keys.items():
            value = table.get(key)
            if isinstance(value, str) and value.strip():
                yield self.fact(kind, value.strip()[:MAX_TEXT], key)

    def keywords(self, table: dict[str, Any]) -> Iterator[Fact]:
        words = table.get("keywords")
        if isinstance(words, list):
            clean: list[JsonValue] = [w[:MAX_TEXT] for w in words if isinstance(w, str)]
            if clean:
                yield self.fact("manifest.keywords", clean, "keywords")


class CargoExtractor:
    name = "cargo"

    def extract(self, files: RepoFiles) -> Iterator[Fact]:
        paths = list(files)
        for path in paths:
            if _name(path) == "Cargo.toml":
                yield from self._manifest(files, path, paths)

    def _manifest(self, files: RepoFiles, path: str, paths: list[str]) -> Iterator[Fact]:
        toml = _Toml(files, path)
        package = _table(toml.data.get("package"))
        if not package:
            return  # a workspace root without a package of its own
        yield from toml.text_facts(
            package,
            {
                "name": "manifest.name",
                "version": "manifest.version",
                "description": "manifest.description",
                "homepage": "manifest.homepage",
            },
        )
        yield from toml.keywords(package)

        platforms: dict[str, str] = {}
        base = path[: -len("Cargo.toml")]
        if toml.data.get("bin") or f"{base}src/main.rs" in paths:
            platforms["cli"] = "name"
        for dependency in sorted(_table(toml.data.get("dependencies"))):
            framework = RUST_FRAMEWORKS.get(dependency)
            if framework:
                yield toml.fact("manifest.framework", framework, dependency)
                platforms[RUST_PLATFORMS.get(dependency, "web")] = dependency
                platforms.pop("cli", None)  # the binary is the app or server, not a CLI tool
        for platform, key in platforms.items():
            yield toml.fact("manifest.platform", platform, key)


class PyprojectExtractor:
    name = "pyproject"

    def extract(self, files: RepoFiles) -> Iterator[Fact]:
        for path in files:
            if _name(path) == "pyproject.toml":
                yield from self._manifest(files, path)

    def _manifest(self, files: RepoFiles, path: str) -> Iterator[Fact]:
        toml = _Toml(files, path)
        poetry = _table(_table(toml.data.get("tool")).get("poetry"))
        project = _table(toml.data.get("project")) or poetry
        if not project:
            return  # tool configuration only
        yield from toml.text_facts(
            project,
            {
                "name": "manifest.name",
                "version": "manifest.version",
                "description": "manifest.description",
                "homepage": "manifest.homepage",
            },
        )
        urls = {k.lower(): v for k, v in _table(project.get("urls")).items() if isinstance(k, str)}
        homepage = urls.get("homepage")
        if isinstance(homepage, str) and not isinstance(project.get("homepage"), str):
            yield toml.fact("manifest.homepage", homepage.strip()[:MAX_TEXT], "Homepage")
        yield from toml.keywords(project)

        dependencies = project.get("dependencies")
        if isinstance(dependencies, dict):  # Poetry
            names = [k for k in dependencies if isinstance(k, str)]
        elif isinstance(dependencies, list):  # PEP 621 requirement strings
            matches = (REQUIREMENT_NAME_RE.match(d) for d in dependencies if isinstance(d, str))
            names = [m.group(1) for m in matches if m]
        else:
            names = []
        web = False
        for dependency in sorted({n.lower().replace("_", "-") for n in names}):
            framework = PYTHON_FRAMEWORKS.get(dependency)
            if framework:
                web = True
                yield Fact(kind="manifest.framework", value=framework, file=path)
        if web:
            yield Fact(kind="manifest.platform", value="web", file=path)
        if _table(project.get("scripts")) or _table(poetry.get("scripts")):
            yield toml.fact("manifest.platform", "cli", "scripts")


class GoModExtractor:
    name = "go_mod"

    def extract(self, files: RepoFiles) -> Iterator[Fact]:
        paths = list(files)
        for path in paths:
            if _name(path) == "go.mod":
                yield from self._manifest(files, path, paths)

    def _manifest(self, files: RepoFiles, path: str, paths: list[str]) -> Iterator[Fact]:
        for number, line in enumerate(files.read_text(path).splitlines(), start=1):
            match = GO_MODULE_RE.match(line)
            if not match:
                continue
            module = match.group(1).strip("\"'")[:MAX_TEXT]
            name = re.sub(r"/v\d+$", "", module).rsplit("/", 1)[-1]
            yield Fact(
                kind="manifest.name", value=name, file=path, start_line=number, end_line=number
            )
            yield Fact(
                kind="manifest.module", value=module, file=path, start_line=number, end_line=number
            )
            break
        else:
            return
        base = path[: -len("go.mod")]
        entry = next(
            (p for p in paths if p == f"{base}main.go" or p.startswith(f"{base}cmd/")), None
        )
        if entry:
            yield Fact(kind="manifest.platform", value="cli", file=entry)
