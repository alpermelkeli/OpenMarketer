import { describe, expect, it } from "vitest";

import type { Proposal } from "@/lib/api/types";

import { postImageOf, proposedImageOf, proposedImageToShow } from "./attachments";

const publish = (payload: Record<string, unknown>, overrides: Partial<Proposal> = {}) =>
  ({
    action_name: "publish_post",
    run_kind: "writing-run",
    payload,
    ...overrides,
  }) as Proposal;

describe("the image a post goes with", () => {
  it("is the asset and the version it was proposed with", () => {
    expect(postImageOf({ asset_id: "a", asset_version_id: "v" })).toEqual({
      assetId: "a",
      versionId: "v",
    });
  });

  it("is none for a post alone", () => {
    expect(postImageOf({ asset_id: null, asset_version_id: null })).toBeNull();
  });
});

describe("the image a proposal asks to publish", () => {
  it("is the payload's version, found through the post it names", () => {
    expect(proposedImageOf(publish({ asset_version_id: "v", content_item_id: "c" }))).toEqual({
      state: "named",
      versionId: "v",
      contentItemId: "c",
    });
  });

  it("is none for a post without an image", () => {
    expect(proposedImageOf(publish({ asset_version_id: null, content_item_id: "c" }))).toEqual({ state: "none" });
  });

  it("is none for any other action", () => {
    const other = publish({ asset_version_id: "v", content_item_id: "c" }, { action_name: "generate_image" });
    expect(proposedImageOf(other)).toEqual({ state: "none" });
  });

  it("cannot be placed when the payload names no post", () => {
    expect(proposedImageOf(publish({ asset_version_id: "v" }))).toEqual({
      state: "unplaceable",
      versionId: "v",
    });
  });
});

describe("the proposed image the screen shows", () => {
  it("is the payload's version under the post's asset", () => {
    expect(proposedImageToShow("v", { asset_id: "a", asset_version_id: "v" })).toEqual({
      assetId: "a",
      versionId: "v",
    });
  });

  it("is none when the post goes with another version", () => {
    expect(proposedImageToShow("v", { asset_id: "a", asset_version_id: "w" })).toBeNull();
  });
});
