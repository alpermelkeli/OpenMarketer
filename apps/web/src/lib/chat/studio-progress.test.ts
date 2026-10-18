import { describe, expect, it } from "vitest";

import type { StudioRun } from "@/lib/api/types";

import type { StudioRunView } from "./events";
import {
  candidateAlt,
  latestCandidate,
  studioOutcomeText,
  studioProgressStatus,
  studioStageText,
} from "./studio-progress";

const ASSET = "55555555-5555-4555-8555-555555555555";
const V1 = "66666666-6666-4666-8666-666666666666";
const V2 = "77777777-7777-4777-8777-777777777777";

function view(over: Partial<StudioRunView> = {}): StudioRunView {
  return {
    id: "s",
    turnId: "t",
    stages: [],
    status: "running",
    assetId: null,
    finalVersionId: null,
    latestVersionId: null,
    needsReview: null,
    reason: null,
    proposal: null,
    firstEventId: 1,
    startedAt: "2026-10-17T12:00:00Z",
    ...over,
  };
}

function stored(status: StudioRun["status"], over: Partial<StudioRun> = {}): StudioRun {
  return { status, error: null, final_asset_id: null, platform: "x_post", ...over } as StudioRun;
}

describe("a studio run's state in its chat", () => {
  it("is what its events say while its record has not ended", () => {
    expect(studioProgressStatus(view(), stored("running")).state).toBe("running");
    expect(studioProgressStatus(view({ status: "finished", assetId: ASSET }), undefined)).toEqual({
      state: "succeeded",
      assetId: ASSET,
      reason: null,
    });
  });

  it("needs review when its finished event says it stopped at its limits", () => {
    expect(studioProgressStatus(view({ status: "finished", needsReview: true }), undefined).state).toBe("needs_review");
  });

  it("is queued while its record is and it has recorded nothing", () => {
    expect(studioProgressStatus(view(), stored("queued")).state).toBe("queued");
  });

  it("is failed, with the record's reason, when the record says it failed without an event", () => {
    expect(studioProgressStatus(view(), stored("failed", { error: "No worker took it." }))).toEqual({
      state: "failed",
      assetId: null,
      reason: "No worker took it.",
    });
  });

  it("takes its asset from the record when no event named it", () => {
    expect(studioProgressStatus(view(), stored("needs_review", { final_asset_id: ASSET }))).toEqual({
      state: "needs_review",
      assetId: ASSET,
      reason: null,
    });
  });
});

describe("the image a studio run's card shows", () => {
  it("is the one it ended with", () => {
    const ended = view({ status: "finished", assetId: ASSET, finalVersionId: V2, latestVersionId: V1 });
    expect(latestCandidate(ended, studioProgressStatus(ended, undefined))).toEqual({
      assetId: ASSET,
      versionId: V2,
      final: true,
    });
  });

  it("is the newest a step made when the record names the asset but no event the final version", () => {
    const seen = view({ latestVersionId: V1 });
    expect(latestCandidate(seen, studioProgressStatus(seen, stored("succeeded", { final_asset_id: ASSET })))).toEqual({
      assetId: ASSET,
      versionId: V1,
      final: false,
    });
  });

  it("is the newest a step made while the run is going, once a step named its asset", () => {
    const running = view({ assetId: ASSET, latestVersionId: V1 });
    expect(latestCandidate(running, studioProgressStatus(running, stored("running")))).toEqual({
      assetId: ASSET,
      versionId: V1,
      final: false,
    });
  });

  it("is none while the run has not named its asset", () => {
    const running = view({ latestVersionId: V1 });
    expect(latestCandidate(running, studioProgressStatus(running, stored("running")))).toBeNull();
  });

  it("is described by the platform it was made for, when the record is in", () => {
    expect(candidateAlt("x_post")).toBe("Image made for an X post");
    expect(candidateAlt(undefined)).toBe("Image made by the studio run");
  });
});

describe("how a studio run's steps read", () => {
  it("says how the image is planned to be made", () => {
    expect(studioStageText({ kind: "briefed", technique: "background_overlay" })).toBe(
      "Planned the image: a generated background with words set in code",
    );
    expect(studioStageText({ kind: "briefed", technique: "new_way" })).toBe("Planned the image: new_way");
  });

  it("says what an operation made, and in which round", () => {
    const made = { kind: "produced", operation: "generate_image", versionId: V1, technique: "generated", ok: true } as const;
    expect(studioStageText({ ...made, round: 2 })).toBe("Generated an image (round 2)");
    expect(studioStageText({ ...made, operation: "edit_image", round: 1, ok: false, versionId: null })).toBe(
      "Could not edit an image (round 1)",
    );
  });

  it("names what a check found wrong", () => {
    expect(studioStageText({ kind: "checked", versionId: V1, passed: false, failures: ["copy"] })).toBe(
      "Checked the image: words that do not read as asked",
    );
    expect(studioStageText({ kind: "checked", versionId: V1, passed: false, failures: ["size", "bytes", "copy"] })).toBe(
      "Checked the image: the wrong size, too large a file and words that do not read as asked",
    );
    expect(studioStageText({ kind: "checked", versionId: V1, passed: true, failures: [] })).toBe(
      "Checked the image: size, format and words are right",
    );
  });

  it("names the criteria the verifier failed it on", () => {
    expect(studioStageText({ kind: "verified", versionId: V1, passed: false, failed: ["legibility", "brand_fit"] })).toBe(
      "Reviewed by the verifier: failed on legibility and fit with the brand",
    );
  });

  it("says which technique it switched to", () => {
    expect(studioStageText({ kind: "technique_switched", from: "generated", to: "background_overlay", round: 2 })).toBe(
      "Switched to a generated background with words set in code",
    );
  });

  it("says how the next round is planned and how the run ended", () => {
    expect(studioStageText({ kind: "edit_planned", round: 2, steps: 1 })).toBe("Planned the edits of round 2: 1 step");
    expect(studioStageText({ kind: "finalized", versionId: V1, round: 3 })).toBe("Chose the image of round 3");
    expect(studioStageText({ kind: "escalated", versionId: V1, reason: "the round limit (4) was hit" })).toBe(
      "Stopped: the round limit (4) was hit",
    );
  });
});

describe("the sentence that ends a studio run's card", () => {
  it("says where the image waits", () => {
    expect(studioOutcomeText({ state: "succeeded", assetId: ASSET, reason: null })).toBe(
      "Done: the image waits in the Studio library for your decision.",
    );
    expect(studioOutcomeText({ state: "needs_review", assetId: ASSET, reason: null })).toContain("Stopped at its limits");
  });

  it("is none while it runs or once it failed", () => {
    expect(studioOutcomeText({ state: "running", assetId: null, reason: null })).toBeNull();
    expect(studioOutcomeText({ state: "failed", assetId: null, reason: "x" })).toBeNull();
  });
});
