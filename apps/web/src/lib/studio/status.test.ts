import { describe, expect, it } from "vitest";

import {
  PLATFORM_OPTIONS,
  assetStatusDisplay,
  formatBytes,
  formatCost,
  isStudioRunFinished,
  isStudioRunWaitingForWorker,
  platformLabel,
  studioRunStatusDisplay,
  techniqueLabel,
} from "./status";

describe("where a studio run stands", () => {
  it("is said in words, with a run that needs review as a warning", () => {
    expect(studioRunStatusDisplay("needs_review")).toEqual({ label: "Needs your review", tone: "warning" });
    expect(studioRunStatusDisplay("failed").tone).toBe("danger");
    expect(studioRunStatusDisplay("running").tone).toBe("progress");
  });

  it("is finished once it succeeded, needs review or failed", () => {
    expect(isStudioRunFinished({ status: "succeeded" })).toBe(true);
    expect(isStudioRunFinished({ status: "needs_review" })).toBe(true);
    expect(isStudioRunFinished({ status: "failed" })).toBe(true);
    expect(isStudioRunFinished({ status: "queued" })).toBe(false);
    expect(isStudioRunFinished({ status: "running" })).toBe(false);
  });

  it("points to a missing worker after a while in the queue", () => {
    const run = { status: "queued" as const, created_at: "2026-10-18T12:00:00Z", finished_at: null };
    const start = Date.parse(run.created_at);
    expect(isStudioRunWaitingForWorker(run, start + 5_000)).toBe(false);
    expect(isStudioRunWaitingForWorker(run, start + 60_000)).toBe(true);
    expect(isStudioRunWaitingForWorker({ ...run, status: "running" }, start + 60_000)).toBe(false);
  });
});

describe("where an asset stands", () => {
  it("is a draft until a person decides, then accepted or rejected", () => {
    expect(assetStatusDisplay("draft")).toEqual({ label: "Draft", tone: "warning" });
    expect(assetStatusDisplay("accepted")).toEqual({ label: "Accepted", tone: "success" });
    expect(assetStatusDisplay("rejected").label).toBe("Rejected");
  });
});

describe("the words for a platform and a technique", () => {
  it("name each platform, and offer every one", () => {
    expect(platformLabel("instagram_story")).toBe("Instagram story");
    expect(PLATFORM_OPTIONS.map((option) => option.value)).toContain("og_image");
    expect(PLATFORM_OPTIONS).toHaveLength(7);
  });

  it("show a platform or a technique the dashboard does not know by its name", () => {
    expect(platformLabel("tiktok_video")).toBe("tiktok_video");
    expect(platformLabel("constructor")).toBe("constructor");
    expect(techniqueLabel("painted")).toBe("painted");
  });

  it("say how a version was made", () => {
    expect(techniqueLabel("composed")).toBe("Composed by code");
    expect(techniqueLabel("html_reference")).toContain("HTML");
  });
});

describe("a cost and a size", () => {
  it("keep the fraction of a cent an image costs", () => {
    expect(formatCost(0.04)).toBe("$0.04");
    expect(formatCost(0.0123)).toBe("$0.0123");
    expect(formatCost(0)).toBe("$0.00");
    expect(formatCost(1.5)).toBe("$1.50");
  });

  it("are in bytes, kilobytes or megabytes", () => {
    expect(formatBytes(812)).toBe("812 bytes");
    expect(formatBytes(421_888)).toBe("412 KB");
    expect(formatBytes(1_258_291)).toBe("1.2 MB");
  });
});
