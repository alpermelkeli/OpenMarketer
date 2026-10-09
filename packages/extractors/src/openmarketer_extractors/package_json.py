"""Extractor for ``package.json`` manifests (Node.js, Next.js, React Native, ...).

Facts produced, one set per manifest found (monorepos have several):

    manifest.name, manifest.description, manifest.version,
    manifest.homepage, manifest.keywords
    manifest.framework   a known framework among the dependencies
    manifest.platform    web | ios | android | desktop | cli, implied by the above
    sdk.analytics        analytics SDK among the dependencies
    sdk.payments         payment or subscription SDK among the dependencies
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from fnmatch import fnmatch
from typing import Any

from openmarketer_core.repository_analysis.extraction import Fact
from openmarketer_core.repository_analysis.intake import RepoFiles

MAX_TEXT = 500
DEPENDENCY_SECTIONS = ("dependencies", "devDependencies", "peerDependencies")

# dependency -> (framework name, platforms it implies)
FRAMEWORKS: dict[str, tuple[str, tuple[str, ...]]] = {
    "next": ("nextjs", ("web",)),
    "nuxt": ("nuxt", ("web",)),
    "@sveltejs/kit": ("sveltekit", ("web",)),
    "@angular/core": ("angular", ("web",)),
    "@remix-run/react": ("remix", ("web",)),
    "astro": ("astro", ("web",)),
    "gatsby": ("gatsby", ("web",)),
    "vue": ("vue", ("web",)),
    "svelte": ("svelte", ("web",)),
    "react-dom": ("react", ("web",)),
    "expo": ("expo", ("ios", "android")),
    "react-native": ("react_native", ("ios", "android")),
    "electron": ("electron", ("desktop",)),
    "@tauri-apps/api": ("tauri", ("desktop",)),
}

# fact kind -> {dependency name pattern: SDK name}
SDKS: dict[str, dict[str, str]] = {
    "sdk.analytics": {
        "firebase": "firebase",
        "@react-native-firebase/analytics": "firebase",
        "posthog-*": "posthog",
        "mixpanel*": "mixpanel",
        "@amplitude/*": "amplitude",
        "@segment/*": "segment",
        "@vercel/analytics": "vercel_analytics",
        "plausible-tracker": "plausible",
    },
    "sdk.payments": {
        "stripe": "stripe",
        "@stripe/*": "stripe",
        "react-native-purchases": "revenuecat",
        "react-native-iap": "store_iap",
        "@paddle/*": "paddle",
        "@lemonsqueezy/*": "lemonsqueezy",
    },
}


def _line_of(lines: list[str], key: str) -> int | None:
    """1-based line of the first ``"key":`` in the file, if any."""
    pattern = re.compile(rf'^\s*"{re.escape(key)}"\s*:')
    return next((i for i, line in enumerate(lines, start=1) if pattern.match(line)), None)


class PackageJsonExtractor:
    name = "package_json"

    def extract(self, files: RepoFiles) -> Iterator[Fact]:
        for path in files:
            if path.rsplit("/", 1)[-1] == "package.json":
                yield from self._extract_manifest(files, path)

    def _extract_manifest(self, files: RepoFiles, path: str) -> Iterator[Fact]:
        text = files.read_text(path)
        try:
            manifest = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(manifest, dict):
            return
        lines = text.splitlines()

        def fact(kind: str, value: Any, key: str) -> Fact:
            line = _line_of(lines, key)
            return Fact(kind=kind, value=value, file=path, start_line=line, end_line=line)

        for key in ("name", "description", "version", "homepage"):
            value = manifest.get(key)
            if isinstance(value, str) and value.strip():
                yield fact(f"manifest.{key}", value.strip()[:MAX_TEXT], key)

        keywords = manifest.get("keywords")
        if isinstance(keywords, list):
            words = [k[:MAX_TEXT] for k in keywords if isinstance(k, str)]
            if words:
                yield fact("manifest.keywords", words, "keywords")

        dependencies: set[str] = set()
        for section in DEPENDENCY_SECTIONS:
            block = manifest.get(section)
            if isinstance(block, dict):
                dependencies.update(k for k in block if isinstance(k, str))

        platforms: dict[str, str] = {}  # platform -> key that implies it
        for dependency, (framework, implied) in FRAMEWORKS.items():
            if dependency in dependencies:
                yield fact("manifest.framework", framework, dependency)
                for platform in implied:
                    platforms.setdefault(platform, dependency)
        if manifest.get("bin"):
            platforms.setdefault("cli", "bin")
        for platform, key in platforms.items():
            yield fact("manifest.platform", platform, key)

        for kind, patterns in SDKS.items():
            seen: set[str] = set()
            for dependency in sorted(dependencies):
                sdk = next((s for p, s in patterns.items() if fnmatch(dependency, p)), None)
                if sdk and sdk not in seen:
                    seen.add(sdk)
                    yield fact(kind, sdk, dependency)
