import { describe, expect, it } from "vitest";

import { assetImageAlt, versionDownloadHref, versionImageSrc } from "./image-src";

const PROJECT = "8ce5e2bb-6e54-49b4-9ddc-b96f20ec1109";
const ASSET = "0b0e6c6e-3333-4444-8555-666677778888";
const VERSION = "0b0e6c6e-9999-4aaa-8bbb-ccccddddeeee";

describe("the address of a version's image", () => {
  it("is built under the dashboard's proxy from the ids", () => {
    expect(versionImageSrc({ projectId: PROJECT, assetId: ASSET, versionId: VERSION })).toBe(
      `/api/v1/projects/${PROJECT}/media-assets/${ASSET}/versions/${VERSION}/image`,
    );
    expect(versionDownloadHref({ projectId: PROJECT, assetId: ASSET, versionId: VERSION })).toBe(
      `/api/v1/projects/${PROJECT}/media-assets/${ASSET}/versions/${VERSION}/download`,
    );
  });

  it("is none when an id is not one, whatever it holds", () => {
    for (const versionId of ["https://evil.example/x.png", "../../settings", `${VERSION}/../x`, ""]) {
      expect(versionImageSrc({ projectId: PROJECT, assetId: ASSET, versionId })).toBeNull();
    }
    expect(versionDownloadHref({ projectId: "javascript:alert(1)", assetId: ASSET, versionId: VERSION })).toBeNull();
  });
});

describe("the text that stands for an image", () => {
  it("says what it was made for and which candidate it is", () => {
    expect(assetImageAlt("instagram_post", { draft: true })).toBe("Draft image made for an Instagram post");
    expect(assetImageAlt("linkedin_post")).toBe("Image made for a LinkedIn post");
    expect(assetImageAlt("x_post", { round: 2 })).toBe("Image made for an X post, a candidate of round 2");
  });

  it("says the goal it was asked to meet, when the screen has it", () => {
    expect(assetImageAlt("x_post", { round: 1, goal: " Announce the 2.0 release " })).toBe(
      "Image made for an X post, a candidate of round 1. Asked for: Announce the 2.0 release",
    );
    expect(assetImageAlt("x_post", { goal: "  " })).toBe("Image made for an X post");
  });
});
