import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { MediaAsset, MediaAssetVersion, RepositoryImage } from "@/lib/api/types";

import { AssetReview } from "./asset-review";
import { RepositoryImageGrid } from "./repository-image-grid";

// The proxy refuses a navigation to it (a new tab, a followed link): an image or a file of the API
// is read by the page itself, so nothing drawn may link to an address under /api/v1.
const API_LINK = /href="\/api\/v1\//;

const PROJECT = "8ce5e2bb-6e54-49b4-9ddc-b96f20ec1109";
const IMAGE = "0b0e6c6e-1111-4222-8333-444455556666";
const ASSET = "0b0e6c6e-3333-4444-8555-666677778888";
const VERSION = "0b0e6c6e-9999-4aaa-8bbb-ccccddddeeee";

const image = {
  id: IMAGE,
  analysis_run_id: null,
  path: "docs/screenshot.png",
  kind: "screenshot",
  mime: "image/png",
  width: 1280,
  height: 720,
  size_bytes: 1000,
  source: "repository",
  kind_chosen: false,
} as unknown as RepositoryImage;

const version = {
  id: VERSION,
  round: 1,
  mime: "image/png",
  width: 1080,
  height: 1080,
} as unknown as MediaAssetVersion;

function asset(status: "draft" | "accepted"): MediaAsset {
  return {
    id: ASSET,
    project_id: PROJECT,
    platform: "instagram_post",
    status,
    current_version_id: VERSION,
    decided_at: null,
  } as unknown as MediaAsset;
}

function drawGrid(): string {
  const client = new QueryClient();
  return renderToStaticMarkup(
    createElement(QueryClientProvider, { client }, createElement(RepositoryImageGrid, { projectId: PROJECT, images: [image] })),
  );
}

function drawReview(status: "draft" | "accepted"): string {
  return renderToStaticMarkup(
    createElement(AssetReview, {
      asset: asset(status),
      version,
      totalCostUsd: 0,
      decidable: true,
      deciding: null,
      problem: null,
      onAccept: () => {},
      onReject: () => {},
      rejectOpen: false,
      onRejectOpenChange: () => {},
      downloading: false,
      downloadProblem: null,
      onDownload: () => {},
    }),
  );
}

describe("a repository image's tile", () => {
  it("shows the image from the proxy without linking to it", () => {
    const html = drawGrid();
    expect(html).toContain(`/api/v1/projects/${PROJECT}/repository-images/${IMAGE}/image`);
    expect(html).not.toMatch(API_LINK);
    expect(html).not.toContain('target="_blank"');
  });

  it("opens the viewer from a button", () => {
    expect(drawGrid()).toContain("opens the image at full size");
    expect(drawGrid()).toMatch(/<button[^>]*type="button"/);
  });
});

describe("the image a run ended with", () => {
  it.each(["draft", "accepted"] as const)("is downloaded from a button, not a link, while %s", (status) => {
    const html = drawReview(status);
    expect(html).toContain("Download");
    expect(html).not.toMatch(API_LINK);
    expect(html).not.toContain("download=");
  });
});
