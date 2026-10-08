"""Tests for the Flutter extractor."""

import pytest

from openmarketer_core.intake import RepoFiles
from openmarketer_extractors.flutter import FlutterExtractor

PUBSPEC = """\
name: habit_garden
description: Grow habits, one day at a time.
version: 2.1.0+34
homepage: https://habitgarden.example

environment:
  sdk: ^3.5.0

dependencies:
  flutter:
    sdk: flutter
  firebase_analytics: ^11.0.0
  purchases_flutter: ^8.0.0
  google_mobile_ads: ^5.0.0

dev_dependencies:
  flutter_test:
    sdk: flutter

flutter:
  uses-material-design: true
"""


def extract(tmp_path, files: dict[str, str]):
    for path, content in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return list(FlutterExtractor().extract(RepoFiles(tmp_path)))


def values(facts, kind):
    return [f.value for f in facts if f.kind == kind]


def test_flutter_app(tmp_path):
    facts = extract(
        tmp_path,
        {
            "pubspec.yaml": PUBSPEC,
            "android/app/build.gradle": "",
            "ios/Runner/Info.plist": "",
            "lib/main.dart": "",
        },
    )
    assert values(facts, "manifest.name") == ["habit_garden"]
    assert values(facts, "manifest.description") == ["Grow habits, one day at a time."]
    assert values(facts, "manifest.version") == ["2.1.0+34"]
    assert values(facts, "manifest.framework") == ["flutter"]
    assert values(facts, "manifest.platform") == ["android", "ios"]
    assert values(facts, "sdk.analytics") == ["firebase"]
    assert values(facts, "sdk.payments") == ["revenuecat"]
    assert values(facts, "sdk.ads") == ["admob"]
    analytics = next(f for f in facts if f.kind == "sdk.analytics")
    assert (analytics.file, analytics.start_line) == ("pubspec.yaml", 12)


def test_platform_folders_are_relative_to_the_pubspec(tmp_path):
    facts = extract(
        tmp_path,
        {"apps/mobile/pubspec.yaml": PUBSPEC, "apps/mobile/web/index.html": "", "ios/x": ""},
    )
    assert values(facts, "manifest.platform") == ["web"]


def test_plain_dart_package(tmp_path):
    facts = extract(tmp_path, {"pubspec.yaml": "name: tidy\ndependencies:\n  args: ^2.0.0\n"})
    assert values(facts, "manifest.framework") == ["dart"]
    assert values(facts, "manifest.platform") == []


@pytest.mark.parametrize("content", ["name: [unclosed", "- just\n- a list\n", ""])
def test_malformed_pubspecs_are_skipped(tmp_path, content):
    assert extract(tmp_path, {"pubspec.yaml": content}) == []


def test_oversized_pubspec_is_skipped(tmp_path):
    assert extract(tmp_path, {"pubspec.yaml": PUBSPEC + "# pad\n" * 120_000}) == []
