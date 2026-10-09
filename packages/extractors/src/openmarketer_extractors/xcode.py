"""Extractor for Xcode projects (``project.pbxproj``).

Native Apple apps keep their identity in build settings, and recent Xcode
versions also generate Info.plist from them, so this is where the bundle
identifier, version, platforms and permission texts live.

Facts produced:

    manifest.name        INFOPLIST_KEY_CFBundleDisplayName
    manifest.bundle_id   PRODUCT_BUNDLE_IDENTIFIER (test targets and build variables skipped)
    manifest.version     MARKETING_VERSION
    manifest.platform    ios | macos | tvos | watchos | visionos
    ios.permission       {"key": "NSCameraUsageDescription", "purpose": "..."}
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from pydantic import JsonValue

from openmarketer_core.repository_analysis.extraction import Fact
from openmarketer_core.repository_analysis.intake import RepoFiles

MAX_TEXT = 500
SETTING_RE = re.compile(
    r"""^\s*([A-Za-z_][\w\[\]=*]*)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;"]*));\s*$"""
)
USAGE_KEY_RE = re.compile(r"^INFOPLIST_KEY_(NS\w+UsageDescription)$")

SDK_PLATFORMS = {
    "iphoneos": "ios",
    "iphonesimulator": "ios",
    "macosx": "macos",
    "appletvos": "tvos",
    "appletvsimulator": "tvos",
    "watchos": "watchos",
    "watchsimulator": "watchos",
    "xros": "visionos",
    "xrsimulator": "visionos",
}


class XcodeExtractor:
    name = "xcode"

    def extract(self, files: RepoFiles) -> Iterator[Fact]:
        for path in files:
            if path.endswith(".xcodeproj/project.pbxproj"):
                yield from self._project(files, path)

    def _project(self, files: RepoFiles, path: str) -> Iterator[Fact]:
        seen: set[tuple[str, str]] = set()

        def fact(kind: str, value: JsonValue, number: int) -> Iterator[Fact]:
            marker = (kind, str(value))
            if marker not in seen:
                seen.add(marker)
                yield Fact(kind=kind, value=value, file=path, start_line=number, end_line=number)

        for number, line in enumerate(files.read_text(path).splitlines(), start=1):
            match = SETTING_RE.match(line)
            if not match:
                continue
            key = match.group(1)
            value = (match.group(2) if match.group(2) is not None else match.group(3)).strip()
            value = value.replace('\\"', '"')[:MAX_TEXT]
            if not value:
                continue
            is_variable = "$" in value

            if key == "PRODUCT_BUNDLE_IDENTIFIER" and not is_variable:
                if not re.search(r"tests?$", value, re.IGNORECASE):
                    yield from fact("manifest.bundle_id", value, number)
            elif key == "MARKETING_VERSION" and not is_variable:
                yield from fact("manifest.version", value, number)
            elif key == "INFOPLIST_KEY_CFBundleDisplayName" and not is_variable:
                yield from fact("manifest.name", value, number)
            elif key in ("SDKROOT", "SUPPORTED_PLATFORMS"):
                for sdk in value.split():
                    platform = SDK_PLATFORMS.get(sdk)
                    if platform:
                        yield from fact("manifest.platform", platform, number)
            else:
                usage = USAGE_KEY_RE.match(key)
                if usage:
                    yield from fact(
                        "ios.permission", {"key": usage.group(1), "purpose": value}, number
                    )
