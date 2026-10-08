"""Tests for the Xcode project extractor."""

from openmarketer_core.intake import RepoFiles
from openmarketer_extractors.xcode import XcodeExtractor

PBXPROJ = """\
// !$*UTF8*$!
{
	objects = {
		AAA /* Debug */ = {
			buildSettings = {
				INFOPLIST_KEY_CFBundleDisplayName = "Gif Maker";
				INFOPLIST_KEY_NSCameraUsageDescription = "Record a clip to turn into a \\"GIF\\".";
				MARKETING_VERSION = 2.23.1;
				PRODUCT_BUNDLE_IDENTIFIER = com.example.GifMaker;
				PRODUCT_NAME = "$(TARGET_NAME)";
				SDKROOT = macosx;
			};
		};
		BBB /* Release */ = {
			buildSettings = {
				MARKETING_VERSION = 2.23.1;
				PRODUCT_BUNDLE_IDENTIFIER = com.example.GifMaker;
				SUPPORTED_PLATFORMS = "iphoneos iphonesimulator xros";
			};
		};
		CCC /* Tests */ = {
			buildSettings = {
				PRODUCT_BUNDLE_IDENTIFIER = com.example.GifMakerTests;
				PRODUCT_BUNDLE_IDENTIFIER = "$(BASE_ID).share";
			};
		};
	};
}
"""


def extract(tmp_path, path="App/GifMaker.xcodeproj/project.pbxproj", content=PBXPROJ):
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return list(XcodeExtractor().extract(RepoFiles(tmp_path)))


def found(facts, kind):
    return [(f.value, f.start_line) for f in facts if f.kind == kind]


def test_native_apple_app(tmp_path):
    facts = extract(tmp_path)
    assert found(facts, "manifest.name") == [("Gif Maker", 6)]
    assert found(facts, "manifest.version") == [("2.23.1", 8)]
    assert found(facts, "manifest.bundle_id") == [("com.example.GifMaker", 9)]
    assert found(facts, "manifest.platform") == [("macos", 11), ("ios", 18), ("visionos", 18)]
    assert found(facts, "ios.permission") == [
        (
            {
                "key": "NSCameraUsageDescription",
                "purpose": 'Record a clip to turn into a "GIF".',
            },
            7,
        )
    ]


def test_other_pbxproj_locations_and_garbage_are_ignored(tmp_path):
    assert extract(tmp_path, path="notes/project.pbxproj") == []
    assert extract(tmp_path, content="\x00\x01 not a project ;;; = ;\n") == []
