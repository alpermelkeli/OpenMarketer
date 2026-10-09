"""Extractor for Flutter and Dart projects (``pubspec.yaml``).

Facts produced, one set per pubspec found:

    manifest.name, manifest.description, manifest.version, manifest.homepage
    manifest.framework   flutter | dart
    manifest.platform    android | ios | web | macos | windows | linux, from the
                         platform folders next to the pubspec
    sdk.analytics, sdk.payments, sdk.ads   known packages among the dependencies
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import yaml

from openmarketer_core.repository_analysis.extraction import Fact
from openmarketer_core.repository_analysis.intake import RepoFiles

MAX_TEXT = 500
MAX_BYTES = 512 * 1024
PLATFORM_DIRS = ("android", "ios", "web", "macos", "windows", "linux")

# fact kind -> {pub package: SDK name}
SDKS: dict[str, dict[str, str]] = {
    "sdk.analytics": {
        "firebase_analytics": "firebase",
        "posthog_flutter": "posthog",
        "mixpanel_flutter": "mixpanel",
        "amplitude_flutter": "amplitude",
        "flutter_segment": "segment",
    },
    "sdk.payments": {
        "purchases_flutter": "revenuecat",
        "in_app_purchase": "store_iap",
        "flutter_stripe": "stripe",
    },
    "sdk.ads": {
        "google_mobile_ads": "admob",
        "applovin_max": "applovin",
        "unity_ads_plugin": "unity_ads",
    },
}


def _line_of(lines: list[str], key: str) -> int | None:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:")
    return next((i for i, line in enumerate(lines, start=1) if pattern.match(line)), None)


class FlutterExtractor:
    name = "flutter"

    def extract(self, files: RepoFiles) -> Iterator[Fact]:
        paths = list(files)
        for path in paths:
            if path.rsplit("/", 1)[-1] == "pubspec.yaml":
                yield from self._pubspec(files, path, paths)

    def _pubspec(self, files: RepoFiles, path: str, paths: list[str]) -> Iterator[Fact]:
        data = files.read_bytes(path)
        if len(data) > MAX_BYTES:
            return
        text = data.decode("utf-8", errors="replace")
        try:
            pubspec = yaml.safe_load(text)
        except yaml.YAMLError:
            return
        if not isinstance(pubspec, dict):
            return
        lines = text.splitlines()

        def fact(kind: str, value: Any, key: str) -> Fact:
            line = _line_of(lines, key)
            return Fact(kind=kind, value=value, file=path, start_line=line, end_line=line)

        for key in ("name", "description", "version", "homepage"):
            value = pubspec.get(key)
            if isinstance(value, str) and value.strip():
                yield fact(f"manifest.{key}", value.strip()[:MAX_TEXT], key)

        dependencies: set[str] = set()
        for section in ("dependencies", "dev_dependencies"):
            block = pubspec.get(section)
            if isinstance(block, dict):
                dependencies.update(k for k in block if isinstance(k, str))

        is_flutter = "flutter" in dependencies or isinstance(pubspec.get("flutter"), dict)
        yield fact("manifest.framework", "flutter" if is_flutter else "dart", "flutter")

        if is_flutter:
            base = path[: -len("pubspec.yaml")]
            for platform in PLATFORM_DIRS:
                prefix = f"{base}{platform}/"
                marker = next((p for p in paths if p.startswith(prefix)), None)
                if marker:
                    yield Fact(kind="manifest.platform", value=platform, file=marker)

        for kind, packages in SDKS.items():
            seen: set[str] = set()
            for dependency in sorted(dependencies):
                sdk = packages.get(dependency)
                if sdk and sdk not in seen:
                    seen.add(sdk)
                    yield fact(kind, sdk, dependency)
