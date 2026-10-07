"""Extractor for Expo configuration (``app.json``).

React Native dependencies are already covered by the package.json extractor;
this adds what only the Expo config knows.

Facts produced:

    manifest.name, manifest.version, manifest.description
    manifest.application_id   android.package
    manifest.bundle_id        ios.bundleIdentifier
    manifest.platform         from "platforms", or from the ios/android/web sections
    manifest.url_scheme       deep link scheme
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from openmarketer_core.extraction import Fact
from openmarketer_core.intake import RepoFiles

MAX_TEXT = 500
PLATFORMS = ("ios", "android", "web")


def _line_of(lines: list[str], key: str) -> int | None:
    pattern = re.compile(rf'^\s*"{re.escape(key)}"\s*:')
    return next((i for i, line in enumerate(lines, start=1) if pattern.match(line)), None)


class ExpoExtractor:
    name = "expo"

    def extract(self, files: RepoFiles) -> Iterator[Fact]:
        for path in files:
            if path.rsplit("/", 1)[-1] == "app.json":
                yield from self._app_json(files, path)

    def _app_json(self, files: RepoFiles, path: str) -> Iterator[Fact]:
        text = files.read_text(path)
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            return
        expo = document.get("expo") if isinstance(document, dict) else None
        if not isinstance(expo, dict):
            return  # some other tool's app.json
        lines = text.splitlines()

        def fact(kind: str, value: Any, key: str) -> Iterator[Fact]:
            if isinstance(value, str) and value.strip():
                line = _line_of(lines, key)
                yield Fact(
                    kind=kind,
                    value=value.strip()[:MAX_TEXT],
                    file=path,
                    start_line=line,
                    end_line=line,
                )

        yield from fact("manifest.name", expo.get("name"), "name")
        yield from fact("manifest.version", expo.get("version"), "version")
        yield from fact("manifest.description", expo.get("description"), "description")
        yield from fact("manifest.url_scheme", expo.get("scheme"), "scheme")

        def section(key: str) -> dict[str, Any]:
            value = expo.get(key)
            return value if isinstance(value, dict) else {}

        ios, android = section("ios"), section("android")
        yield from fact("manifest.bundle_id", ios.get("bundleIdentifier"), "bundleIdentifier")
        yield from fact("manifest.application_id", android.get("package"), "package")

        declared = expo.get("platforms")
        if isinstance(declared, list):
            platforms = [p for p in PLATFORMS if p in declared]
            key_for = dict.fromkeys(platforms, "platforms")
        else:
            platforms = [p for p in PLATFORMS if isinstance(expo.get(p), dict)]
            key_for = {p: p for p in platforms}
        for platform in platforms:
            yield from fact("manifest.platform", platform, key_for[platform])
