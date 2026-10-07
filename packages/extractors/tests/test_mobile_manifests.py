"""Tests for the AndroidManifest.xml and Info.plist extractors."""

import pytest

from openmarketer_core.intake import RepoFiles
from openmarketer_extractors.mobile_manifests import AndroidManifestExtractor, InfoPlistExtractor

ANDROID_MANIFEST = """\
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.CAMERA"/>
    <uses-permission android:name="android.permission.CAMERA"/>
    <uses-permission-sdk-23 android:name="android.permission.POST_NOTIFICATIONS"/>
    <uses-permission android:name="com.android.vending.BILLING"/>
    <uses-feature android:name="android.hardware.camera" android:required="true" />
    <application android:label="@string/app_name" />
</manifest>
"""

STRINGS = """\
<resources>
    <string name="app_name">Tom &amp; Jerry</string>
    <string name="other">x</string>
</resources>
"""

INFO_PLIST = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDisplayName</key>
	<string>Memoria</string>
	<key>NSCameraUsageDescription</key>
	<string>Camera access needed to take photos</string>
	<key>NSPhotoLibraryUsageDescription</key>
	<string>Photo library access needed to select images</string>
	<key>UIRequiresFullScreen</key>
	<true/>
</dict>
</plist>
"""


def write(tmp_path, files: dict[str, str]) -> RepoFiles:
    for path, content in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return RepoFiles(tmp_path)


def found(facts, kind):
    return [(f.value, f.start_line) for f in facts if f.kind == kind]


# ---------------------------------------------------------------- android
def test_android_permissions_and_features(tmp_path):
    files = write(tmp_path, {"app/src/main/AndroidManifest.xml": ANDROID_MANIFEST})
    facts = list(AndroidManifestExtractor().extract(files))
    assert found(facts, "android.permission") == [
        ("CAMERA", 3),
        ("POST_NOTIFICATIONS", 5),
        ("com.android.vending.BILLING", 6),
    ]
    assert found(facts, "android.feature") == [("android.hardware.camera", 7)]


def test_app_name_from_default_strings(tmp_path):
    files = write(
        tmp_path,
        {
            "app/src/main/res/values/strings.xml": STRINGS,
            "app/src/main/res/values-tr/strings.xml": STRINGS.replace("Tom", "Tomi"),
        },
    )
    facts = list(AndroidManifestExtractor().extract(files))
    assert found(facts, "manifest.name") == [("Tom & Jerry", 2)]


# -------------------------------------------------------------------- ios
def test_info_plist_name_and_permissions(tmp_path):
    files = write(tmp_path, {"iosApp/iosApp/Info.plist": INFO_PLIST})
    facts = list(InfoPlistExtractor().extract(files))
    assert found(facts, "manifest.name") == [("Memoria", 5)]
    assert found(facts, "ios.permission") == [
        ({"key": "NSCameraUsageDescription", "purpose": "Camera access needed to take photos"}, 7),
        (
            {
                "key": "NSPhotoLibraryUsageDescription",
                "purpose": "Photo library access needed to select images",
            },
            9,
        ),
    ]


def test_build_variable_is_not_a_name(tmp_path):
    plist = INFO_PLIST.replace("<string>Memoria</string>", "<string>$(PRODUCT_NAME)</string>")
    facts = list(InfoPlistExtractor().extract(write(tmp_path, {"Info.plist": plist})))
    assert found(facts, "manifest.name") == []


@pytest.mark.parametrize(
    "content",
    [
        "not a plist",
        "",
        '<?xml version="1.0"?><plist version="1.0"><array><string>x</string></array></plist>',
        '<?xml version="1.0"?><!DOCTYPE plist [<!ENTITY a "aaaa">]>'
        '<plist version="1.0"><dict><key>CFBundleName</key><string>&a;</string></dict></plist>',
    ],
)
def test_malformed_or_hostile_plists_are_skipped(tmp_path, content):
    facts = list(InfoPlistExtractor().extract(write(tmp_path, {"Info.plist": content})))
    assert facts == []
