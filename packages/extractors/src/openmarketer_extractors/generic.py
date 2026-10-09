"""Extractor that works on any repository, whatever it is built with.

It is the floor under the stack-specific extractors: a repository in a stack
nobody wrote an extractor for still yields its languages, its README, its
licence and pointers to the manifest files the synthesis step should read.

Facts produced:

    repo.language   {"name": "Kotlin", "files": 121}, most used first
    repo.readme     {"title": "...", "summary": "..."} from the root README
    repo.license    name of the licence file at the root
    repo.manifest   {"ecosystem": "cargo"}, one per recognised manifest file
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from fnmatch import fnmatch

from openmarketer_core.repository_analysis.extraction import Fact, is_auxiliary
from openmarketer_core.repository_analysis.intake import RepoFiles

MAX_TEXT = 500
MAX_LANGUAGES = 8
MAX_MANIFESTS = 50

LANGUAGES = {
    "kt": "Kotlin", "kts": "Kotlin", "java": "Java", "swift": "Swift", "m": "Objective-C",
    "mm": "Objective-C", "dart": "Dart", "ts": "TypeScript", "tsx": "TypeScript",
    "js": "JavaScript", "jsx": "JavaScript", "mjs": "JavaScript", "vue": "Vue",
    "svelte": "Svelte", "py": "Python", "go": "Go", "rs": "Rust", "rb": "Ruby", "php": "PHP",
    "cs": "C#", "fs": "F#", "c": "C", "h": "C", "cpp": "C++", "cc": "C++", "hpp": "C++",
    "ex": "Elixir", "exs": "Elixir", "scala": "Scala", "lua": "Lua", "gd": "GDScript",
    "sh": "Shell", "sql": "SQL", "html": "HTML", "css": "CSS", "scss": "CSS",
}  # fmt: skip

# file name pattern -> ecosystem
MANIFESTS = {
    "package.json": "npm",
    "pubspec.yaml": "flutter",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "pom.xml": "maven",
    "Package.swift": "swift_package",
    "project.pbxproj": "xcode",
    "Podfile": "cocoapods",
    "pyproject.toml": "python",
    "requirements.txt": "python",
    "setup.py": "python",
    "Cargo.toml": "cargo",
    "go.mod": "go",
    "Gemfile": "ruby",
    "composer.json": "php",
    "*.csproj": "dotnet",
    "*.sln": "dotnet",
    "mix.exs": "elixir",
    "CMakeLists.txt": "cmake",
    "project.godot": "godot",
    "ProjectSettings.asset": "unity",
    "*.uproject": "unreal",
    "manifest.json": "web_manifest",
    "Dockerfile": "docker",
    "app.json": "expo",
}

HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
NOISE_RE = re.compile(r"^\s*(!\[|\[!\[|---|===|\|)")  # badges, rules, tables
HTML_H1_RE = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


def _keep_newlines(match: re.Match[str]) -> str:
    """Blank out a match but keep its line breaks, so line numbers stay valid."""
    return "\n" * match.group(0).count("\n")


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


class GenericExtractor:
    name = "generic"

    def extract(self, files: RepoFiles) -> Iterator[Fact]:
        paths = list(files)
        yield from self._languages(paths)
        yield from self._readme(files, paths)
        yield from self._license(paths)
        yield from self._manifests(paths)

    def _languages(self, paths: list[str]) -> Iterator[Fact]:
        counts: Counter[str] = Counter()
        example: dict[str, str] = {}
        for path in paths:
            name = _name(path)
            language = LANGUAGES.get(name.rsplit(".", 1)[-1].lower()) if "." in name else None
            if language:
                counts[language] += 1
                example.setdefault(language, path)
        for language, count in counts.most_common(MAX_LANGUAGES):
            yield Fact(
                kind="repo.language",
                value={"name": language, "files": count},
                file=example[language],
            )

    def _readme(self, files: RepoFiles, paths: list[str]) -> Iterator[Fact]:
        candidates = sorted(
            (p for p in paths if "/" not in p and p.lower().startswith("readme")),
            key=lambda p: (len(p), p),  # README.md before README-something.md
        )
        if not candidates:
            return
        path = candidates[0]
        title: str | None = None
        title_line: int | None = None
        summary: list[str] = []
        in_code = False
        text = HTML_COMMENT_RE.sub(_keep_newlines, files.read_text(path))
        html_title = HTML_H1_RE.search(text)
        if html_title:  # READMEs that open with an HTML header block
            title = " ".join(TAG_RE.sub(" ", html_title.group(1)).split())
            title_line = text.count("\n", 0, html_title.start()) + 1
            text = HTML_H1_RE.sub(_keep_newlines, text, count=1)
        for number, raw in enumerate(text.splitlines(), start=1):
            line = raw if raw.strip().startswith("```") else TAG_RE.sub("", raw)
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                continue
            heading = HEADING_RE.match(line)
            if heading:
                if title is None:
                    title, title_line = heading.group(1), number
                    continue
                if summary:
                    break
                continue
            if NOISE_RE.match(line):
                continue
            if line.strip():
                summary.append(" ".join(line.split()))
            elif summary:
                break
        if title is None and not summary:
            return
        yield Fact(
            kind="repo.readme",
            value={
                "title": (title or "")[:MAX_TEXT],
                "summary": " ".join(summary)[:MAX_TEXT],
            },
            file=path,
            start_line=title_line,
            end_line=title_line,
        )

    def _license(self, paths: list[str]) -> Iterator[Fact]:
        for path in sorted(paths):
            if "/" not in path and path.lower().split(".")[0] in ("license", "licence", "copying"):
                yield Fact(kind="repo.license", value=path, file=path)
                return

    def _manifests(self, paths: list[str]) -> Iterator[Fact]:
        found = 0
        for path in sorted(paths, key=lambda p: (is_auxiliary(p), p.count("/"), p)):
            name = _name(path)
            ecosystem = next(
                (e for pattern, e in MANIFESTS.items() if fnmatch(name, pattern)), None
            )
            if ecosystem:
                yield Fact(kind="repo.manifest", value={"ecosystem": ecosystem}, file=path)
                found += 1
                if found == MAX_MANIFESTS:
                    return
