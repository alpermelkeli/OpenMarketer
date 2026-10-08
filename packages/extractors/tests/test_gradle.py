"""Tests for the Gradle extractor."""

from openmarketer_core.intake import RepoFiles
from openmarketer_extractors.gradle import GradleExtractor

SETTINGS = 'rootProject.name = "Memoria"\ninclude(":composeApp")\n'

ROOT_BUILD = """\
plugins {
    alias(libs.plugins.androidApplication) apply false
    alias(libs.plugins.kotlinMultiplatform) apply false
}
"""

APP_BUILD = """\
plugins {
    alias(libs.plugins.kotlinMultiplatform)
    alias(libs.plugins.androidApplication)
    alias(libs.plugins.composeMultiplatform)
}

kotlin {
    androidTarget { }
    listOf(iosArm64(), iosSimulatorArm64()).forEach { }
    // jvm("desktop")
}

android {
    defaultConfig {
        applicationId = "com.example.memoria"
        versionName = "1.0"
    }
}

dependencies {
    implementation("com.revenuecat.purchases:purchases:8.0.0")
}
"""

CATALOG = """\
[libraries]
okio = { module = "com.squareup.okio:okio", version = "3.9.0" }
firebase-analytics = { module = "com.google.firebase:firebase-analytics-ktx", version = "22.0.0" }
ads = { group = "com.google.android.gms", name = "play-services-ads", version = "23.0.0" }

[plugins]
androidApplication = { id = "com.android.application", version = "8.11.2" }
kotlinMultiplatform = { id = "org.jetbrains.kotlin.multiplatform", version = "2.3.0" }
composeMultiplatform = { id = "org.jetbrains.compose", version = "1.10.0" }
"""


def extract(tmp_path, files: dict[str, str]):
    for path, content in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return list(GradleExtractor().extract(RepoFiles(tmp_path)))


def found(facts, kind):
    return [(f.value, f.file, f.start_line) for f in facts if f.kind == kind]


KMP_PROJECT = {
    "settings.gradle.kts": SETTINGS,
    "build.gradle.kts": ROOT_BUILD,
    "composeApp/build.gradle.kts": APP_BUILD,
    "gradle/libs.versions.toml": CATALOG,
}


def test_kotlin_multiplatform_project(tmp_path):
    facts = extract(tmp_path, KMP_PROJECT)
    app = "composeApp/build.gradle.kts"
    assert found(facts, "manifest.name") == [("Memoria", "settings.gradle.kts", 1)]
    assert found(facts, "manifest.application_id") == [("com.example.memoria", app, 15)]
    assert found(facts, "manifest.version") == [("1.0", app, 16)]
    assert found(facts, "manifest.framework") == [
        ("kotlin_multiplatform", app, 2),
        ("android", app, 3),
        ("compose_multiplatform", app, 4),
    ]
    assert found(facts, "manifest.platform") == [("android", app, 3), ("ios", app, 9)]


def test_plugins_declared_with_apply_false_are_ignored(tmp_path):
    facts = extract(
        tmp_path, {"build.gradle.kts": ROOT_BUILD, "gradle/libs.versions.toml": CATALOG}
    )
    assert found(facts, "manifest.framework") == []


def test_commented_out_targets_are_ignored(tmp_path):
    facts = extract(tmp_path, KMP_PROJECT)
    assert "desktop" not in [value for value, _, _ in found(facts, "manifest.platform")]


def test_sdks_from_catalog_and_build_file(tmp_path):
    facts = extract(tmp_path, KMP_PROJECT)
    catalog = "gradle/libs.versions.toml"
    assert found(facts, "sdk.analytics") == [("firebase", catalog, 3)]
    assert found(facts, "sdk.ads") == [("admob", catalog, None)]  # group/name form: no single line
    assert found(facts, "sdk.payments") == [("revenuecat", "composeApp/build.gradle.kts", 21)]


def test_groovy_android_project_without_catalog(tmp_path):
    build = """\
plugins {
    id 'com.android.application'
}
android {
    defaultConfig {
        applicationId "com.example.shop"
        versionName "2.3.1"
    }
}
dependencies {
    implementation 'com.android.billingclient:billing:7.0.0'
}
"""
    facts = extract(
        tmp_path, {"settings.gradle": "rootProject.name = 'Shop'\n", "app/build.gradle": build}
    )
    assert found(facts, "manifest.name") == [("Shop", "settings.gradle", 1)]
    assert found(facts, "manifest.application_id") == [("com.example.shop", "app/build.gradle", 6)]
    assert found(facts, "manifest.version") == [("2.3.1", "app/build.gradle", 7)]
    assert found(facts, "manifest.platform") == [("android", "app/build.gradle", 2)]
    assert found(facts, "sdk.payments") == [("store_iap", "app/build.gradle", 11)]


def test_broken_catalog_does_not_stop_the_build_files(tmp_path):
    facts = extract(tmp_path, {**KMP_PROJECT, "gradle/libs.versions.toml": "[libraries\nbroken"})
    assert found(facts, "manifest.name") == [("Memoria", "settings.gradle.kts", 1)]
    assert found(facts, "manifest.framework") == []  # aliases cannot be resolved without it


def test_repository_without_gradle_yields_nothing(tmp_path):
    assert extract(tmp_path, {"README.md": "# hi\n"}) == []
