"""Extractor for Gradle projects (Android, Kotlin Multiplatform, Compose).

Reads ``settings.gradle(.kts)``, ``build.gradle(.kts)`` and the version
catalog ``gradle/libs.versions.toml``. Build scripts are code, so they are
matched line by line and never evaluated.

Facts produced:

    manifest.name             rootProject.name
    manifest.application_id   android applicationId
    manifest.version          android versionName
    manifest.framework        android | kotlin_multiplatform | compose_multiplatform
    manifest.platform         android | ios | desktop | web
    sdk.analytics, sdk.payments, sdk.ads   known SDKs among the dependencies
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator
from fnmatch import fnmatch

from openmarketer_core.extraction import Fact
from openmarketer_core.intake import RepoFiles

MAX_TEXT = 500
SETTINGS_FILES = ("settings.gradle", "settings.gradle.kts")
BUILD_FILES = ("build.gradle", "build.gradle.kts")
CATALOG_FILE = "gradle/libs.versions.toml"

ASSIGNMENTS = {
    "applicationId": "manifest.application_id",
    "versionName": "manifest.version",
}

# Gradle plugin id -> framework
PLUGIN_FRAMEWORKS = {
    "com.android.application": "android",
    "org.jetbrains.kotlin.multiplatform": "kotlin_multiplatform",
    "org.jetbrains.compose": "compose_multiplatform",
}

# Kotlin Multiplatform target declaration -> platform
TARGET_PLATFORMS = {
    r"\bandroidTarget\b": "android",
    r"\bios(Arm64|X64|SimulatorArm64)?\s*\(": "ios",
    r"\bjvm\s*\(": "desktop",
    r"\b(wasmJs|js)\s*[({]": "web",
}

# fact kind -> {"group:artifact" pattern: SDK name}
SDKS: dict[str, dict[str, str]] = {
    "sdk.analytics": {
        "com.google.firebase:firebase-analytics*": "firebase",
        "dev.gitlive:firebase-analytics*": "firebase",
        "com.posthog*:*": "posthog",
        "com.mixpanel.android:*": "mixpanel",
        "com.amplitude:*": "amplitude",
        "com.segment.analytics*:*": "segment",
    },
    "sdk.payments": {
        "com.revenuecat.purchases:*": "revenuecat",
        "com.android.billingclient:*": "store_iap",
        "com.stripe:*": "stripe",
    },
    "sdk.ads": {
        "com.google.android.gms:play-services-ads*": "admob",
        "com.applovin:*": "applovin",
        "com.unity3d.ads:*": "unity_ads",
    },
}

ROOT_NAME_RE = re.compile(r"""rootProject\.name\s*=\s*["']([^"']+)["']""")
PLUGIN_ID_RE = re.compile(r"""\bid\s*\(?\s*["']([\w.\-]+)["']""")
PLUGIN_ALIAS_RE = re.compile(r"\balias\s*\(\s*libs\.plugins\.([\w.]+)\s*\)")
COORDINATE_RE = re.compile(r"""["']([\w.\-]+:[\w.\-]+)(?::[^"']*)?["']""")


def _sdk_for(module: str) -> tuple[str, str] | None:
    for kind, patterns in SDKS.items():
        for pattern, sdk in patterns.items():
            if fnmatch(module, pattern):
                return kind, sdk
    return None


class GradleExtractor:
    name = "gradle"

    def extract(self, files: RepoFiles) -> Iterator[Fact]:
        paths = list(files)
        plugin_aliases, catalog_facts = self._read_catalog(files, paths)
        yield from catalog_facts
        for path in paths:
            name = path.rsplit("/", 1)[-1]
            if name in SETTINGS_FILES:
                yield from self._settings(files, path)
            elif name in BUILD_FILES:
                yield from self._build(files, path, plugin_aliases)

    def _read_catalog(
        self, files: RepoFiles, paths: list[str]
    ) -> tuple[dict[str, str], list[Fact]]:
        """Plugin aliases (accessor -> plugin id) and SDK facts from the version catalog."""
        if CATALOG_FILE not in paths:
            return {}, []
        text = files.read_text(CATALOG_FILE)
        try:
            catalog = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return {}, []
        lines = text.splitlines()

        aliases: dict[str, str] = {}
        plugins = catalog.get("plugins")
        if isinstance(plugins, dict):
            for alias, spec in plugins.items():
                plugin_id = spec.get("id") if isinstance(spec, dict) else spec
                if isinstance(plugin_id, str):
                    # Gradle exposes "my-plugin" and "my_plugin" as libs.plugins.my.plugin
                    aliases[re.sub(r"[-_]", ".", alias)] = plugin_id.split(":")[0]

        facts: list[Fact] = []
        seen: set[tuple[str, str]] = set()
        libraries = catalog.get("libraries")
        if isinstance(libraries, dict):
            for spec in libraries.values():
                module = spec.get("module") if isinstance(spec, dict) else spec
                if isinstance(spec, dict) and module is None and "group" in spec:
                    module = f"{spec.get('group')}:{spec.get('name')}"
                if not isinstance(module, str):
                    continue
                module = ":".join(module.split(":")[:2])
                match = _sdk_for(module)
                if match and match not in seen:
                    seen.add(match)
                    line = next((i for i, text in enumerate(lines, 1) if module in text), None)
                    facts.append(
                        Fact(
                            kind=match[0],
                            value=match[1],
                            file=CATALOG_FILE,
                            start_line=line,
                            end_line=line,
                        )
                    )
        return aliases, facts

    def _settings(self, files: RepoFiles, path: str) -> Iterator[Fact]:
        for number, line in enumerate(files.read_text(path).splitlines(), start=1):
            match = ROOT_NAME_RE.search(line)
            if match:
                yield Fact(
                    kind="manifest.name",
                    value=match.group(1)[:MAX_TEXT],
                    file=path,
                    start_line=number,
                    end_line=number,
                )
                return

    def _build(self, files: RepoFiles, path: str, plugin_aliases: dict[str, str]) -> Iterator[Fact]:
        seen: set[tuple[str, str]] = set()

        def fact(kind: str, value: str, number: int) -> Iterator[Fact]:
            if (kind, value) not in seen:
                seen.add((kind, value))
                yield Fact(kind=kind, value=value, file=path, start_line=number, end_line=number)

        for number, raw in enumerate(files.read_text(path).splitlines(), start=1):
            line = raw.split("//", 1)[0]
            if re.search(r"\bapply\s+false\b", line):
                continue  # declared for subprojects only, not applied here

            for key, kind in ASSIGNMENTS.items():
                match = re.search(rf"""\b{key}\s*=?\s*\(?\s*["']([^"']+)["']""", line)
                if match:
                    yield from fact(kind, match.group(1)[:MAX_TEXT], number)

            plugin_ids = PLUGIN_ID_RE.findall(line)
            plugin_ids += [
                plugin_aliases[a] for a in PLUGIN_ALIAS_RE.findall(line) if a in plugin_aliases
            ]
            for plugin_id in plugin_ids:
                framework = PLUGIN_FRAMEWORKS.get(plugin_id)
                if framework:
                    yield from fact("manifest.framework", framework, number)
                    if framework == "android":
                        yield from fact("manifest.platform", "android", number)

            for pattern, platform in TARGET_PLATFORMS.items():
                if re.search(pattern, line):
                    yield from fact("manifest.platform", platform, number)

            for module in COORDINATE_RE.findall(line):
                sdk = _sdk_for(module)
                if sdk:
                    yield from fact(sdk[0], sdk[1], number)
