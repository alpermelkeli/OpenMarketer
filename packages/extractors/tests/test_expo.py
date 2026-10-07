"""Tests for the Expo extractor."""

import json

from openmarketer_core.intake import RepoFiles
from openmarketer_extractors.expo import ExpoExtractor

APP = {
    "expo": {
        "name": "Trail Mate",
        "slug": "trail-mate",
        "version": "3.0.1",
        "scheme": "trailmate",
        "ios": {"bundleIdentifier": "com.example.trailmate"},
        "android": {"package": "com.example.trailmate.android"},
    }
}


def extract(tmp_path, document) -> list:
    text = document if isinstance(document, str) else json.dumps(document, indent=2)
    (tmp_path / "app.json").write_text(text)
    return list(ExpoExtractor().extract(RepoFiles(tmp_path)))


def values(facts, kind):
    return [f.value for f in facts if f.kind == kind]


def test_expo_app(tmp_path):
    facts = extract(tmp_path, APP)
    assert values(facts, "manifest.name") == ["Trail Mate"]
    assert values(facts, "manifest.version") == ["3.0.1"]
    assert values(facts, "manifest.url_scheme") == ["trailmate"]
    assert values(facts, "manifest.bundle_id") == ["com.example.trailmate"]
    assert values(facts, "manifest.application_id") == ["com.example.trailmate.android"]
    assert values(facts, "manifest.platform") == ["ios", "android"]
    bundle = next(f for f in facts if f.kind == "manifest.bundle_id")
    assert bundle.start_line == 8


def test_declared_platforms_win(tmp_path):
    facts = extract(tmp_path, {"expo": {**APP["expo"], "platforms": ["web", "ios"]}})
    assert values(facts, "manifest.platform") == ["ios", "web"]


def test_app_json_of_another_tool_is_ignored(tmp_path):
    assert extract(tmp_path, {"name": "heroku-app", "env": {}}) == []


def test_malformed_app_json_is_skipped(tmp_path):
    assert extract(tmp_path, "{ nope") == []
    assert extract(tmp_path, {"expo": {"name": 7, "ios": "x", "platforms": "all"}}) == []
