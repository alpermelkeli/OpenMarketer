"""Extractors for the native mobile manifests.

``AndroidManifestExtractor`` reads ``AndroidManifest.xml`` and the default
``strings.xml``:

    android.permission   declared permission, without the "android.permission." prefix
    android.feature      required hardware or software feature
    manifest.name        the app_name string resource

``InfoPlistExtractor`` reads ``Info.plist``:

    manifest.name        CFBundleDisplayName or CFBundleName, unless it is a build variable
    ios.permission       {"key": "NSCameraUsageDescription", "purpose": "..."}

Declared permissions hint at features (camera, location, notifications); the
purpose strings are text the owner already wrote for users.
"""

from __future__ import annotations

import plistlib
import re
from collections.abc import Iterator
from html import unescape

from openmarketer_core.extraction import Fact
from openmarketer_core.intake import RepoFiles

MAX_TEXT = 500

# Matched line by line instead of with an XML parser: the file is untrusted
# and only these attributes are needed, with their line numbers.
PERMISSION_RE = re.compile(
    r"""<uses-permission(?:-sdk-\d+)?\b[^>]*android:name\s*=\s*["']([^"']+)["']"""
)
FEATURE_RE = re.compile(r"""<uses-feature\b[^>]*android:name\s*=\s*["']([^"']+)["']""")
APP_NAME_RE = re.compile(r"""<string\s+name\s*=\s*["']app_name["'][^>]*>([^<]+)</string>""")
USAGE_KEY_RE = re.compile(r"^NS\w+UsageDescription$")
NAME_KEYS = ("CFBundleDisplayName", "CFBundleName")


def _file_name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


class AndroidManifestExtractor:
    name = "android_manifest"

    def extract(self, files: RepoFiles) -> Iterator[Fact]:
        for path in files:
            if _file_name(path) == "AndroidManifest.xml":
                yield from self._manifest(files, path)
            elif path.endswith("res/values/strings.xml"):
                yield from self._strings(files, path)

    def _manifest(self, files: RepoFiles, path: str) -> Iterator[Fact]:
        seen: set[tuple[str, str]] = set()
        for number, line in enumerate(files.read_text(path).splitlines(), start=1):
            for kind, pattern in (
                ("android.permission", PERMISSION_RE),
                ("android.feature", FEATURE_RE),
            ):
                for name in pattern.findall(line):
                    value = name.removeprefix("android.permission.")[:MAX_TEXT]
                    if (kind, value) not in seen:
                        seen.add((kind, value))
                        yield Fact(
                            kind=kind, value=value, file=path, start_line=number, end_line=number
                        )

    def _strings(self, files: RepoFiles, path: str) -> Iterator[Fact]:
        for number, line in enumerate(files.read_text(path).splitlines(), start=1):
            match = APP_NAME_RE.search(line)
            if match:
                value = unescape(match.group(1)).strip()[:MAX_TEXT]
                if value:
                    yield Fact(
                        kind="manifest.name",
                        value=value,
                        file=path,
                        start_line=number,
                        end_line=number,
                    )
                return


class InfoPlistExtractor:
    name = "info_plist"

    def extract(self, files: RepoFiles) -> Iterator[Fact]:
        for path in files:
            if _file_name(path) == "Info.plist":
                yield from self._plist(files, path)

    def _plist(self, files: RepoFiles, path: str) -> Iterator[Fact]:
        data = files.read_bytes(path)
        try:
            plist = plistlib.loads(data)
        except Exception:  # noqa: BLE001  (plistlib raises several unrelated error types)
            return
        if not isinstance(plist, dict):
            return
        lines = data.decode("utf-8", errors="replace").splitlines()

        def line_of(key: str) -> int | None:
            needle = f"<key>{key}</key>"
            return next((i for i, text in enumerate(lines, start=1) if needle in text), None)

        for key in NAME_KEYS:
            value = plist.get(key)
            if isinstance(value, str) and value.strip() and "$(" not in value:
                number = line_of(key)
                yield Fact(
                    kind="manifest.name",
                    value=value.strip()[:MAX_TEXT],
                    file=path,
                    start_line=number,
                    end_line=number,
                )
                break

        for key in sorted(k for k in plist if isinstance(k, str) and USAGE_KEY_RE.match(k)):
            purpose = plist[key]
            if isinstance(purpose, str):
                number = line_of(key)
                yield Fact(
                    kind="ios.permission",
                    value={"key": key, "purpose": purpose.strip()[:MAX_TEXT]},
                    file=path,
                    start_line=number,
                    end_line=number,
                )
