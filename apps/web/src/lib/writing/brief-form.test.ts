import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api/api-error";
import type { Feature, MediaAsset, ProductProfile, ResearchRun } from "@/lib/api/types";

import {
  EMPTY_BRIEF,
  VARIANT_OPTIONS,
  announceableFeatures,
  attachableAssets,
  briefProblems,
  offeredOf,
  usableResearchRuns,
  writingBriefOf,
} from "./brief-form";

describe("the brief the form sends", () => {
  it("is for X, with three texts unless more or fewer are asked for", () => {
    expect(writingBriefOf(EMPTY_BRIEF)).toEqual({ platform: "x", goal: "introduce", variants: 3 });
    expect(VARIANT_OPTIONS).toEqual([1, 2, 3, 4, 5]);
  });

  it("names the feature for an announcement, and only then", () => {
    const form = { ...EMPTY_BRIEF, feature: "csv-export", angle: "left over" };
    expect(writingBriefOf({ ...form, goal: "announce" })).toEqual({
      platform: "x",
      goal: "announce",
      feature: "csv-export",
      variants: 3,
    });
    expect(writingBriefOf({ ...form, goal: "introduce" })).not.toHaveProperty("feature");
  });

  it("sends an announcement without a chosen feature as none, for the API to refuse", () => {
    expect(writingBriefOf({ ...EMPTY_BRIEF, goal: "announce" }).feature).toBeNull();
  });

  it("carries the angle as typed, trimmed, for an angle and only then", () => {
    const form = { ...EMPTY_BRIEF, feature: "csv-export", angle: "  Teams that hate spreadsheets \n" };
    expect(writingBriefOf({ ...form, goal: "angle", variants: 5 })).toEqual({
      platform: "x",
      goal: "angle",
      angle: "Teams that hate spreadsheets",
      variants: 5,
    });
    expect(writingBriefOf({ ...form, goal: "announce" })).not.toHaveProperty("angle");
  });
});

describe("the image and the research the form sends", () => {
  it("are left out when none is chosen", () => {
    const brief = writingBriefOf(EMPTY_BRIEF);
    expect(brief).not.toHaveProperty("asset");
    expect(brief).not.toHaveProperty("research_run_id");
  });

  it("are sent by id when chosen, whatever the goal", () => {
    const form = { ...EMPTY_BRIEF, goal: "angle" as const, angle: "x", asset: "a-1", researchRunId: "r-1" };
    expect(writingBriefOf(form)).toMatchObject({ asset: "a-1", research_run_id: "r-1" });
  });
});

describe("the images a post can go with", () => {
  const asset = (id: string, status: MediaAsset["status"], version: string | null) =>
    ({ id, status, current_version_id: version }) as MediaAsset;

  it("are those not rejected that have an image, drafts among them", () => {
    const assets = [
      asset("a", "draft", "v"),
      asset("b", "accepted", "v"),
      asset("c", "rejected", "v"),
      asset("d", "draft", null),
    ];
    expect(attachableAssets(assets).map((a) => a.id)).toEqual(["a", "b"]);
  });
});

describe("the research a post can draw on", () => {
  it("is the runs that succeeded", () => {
    const runs = (["queued", "running", "succeeded", "failed"] as const).map(
      (status) => ({ id: status, status }) as ResearchRun,
    );
    expect(usableResearchRuns(runs).map((r) => r.id)).toEqual(["succeeded"]);
  });
});

describe("what a choice offers", () => {
  const all = (items: readonly number[]) => [...items];

  it("is being read until its list is", () => {
    expect(offeredOf(undefined, false, all)).toEqual({ state: "reading" });
  });

  it("is unreadable when its list failed, not an empty list", () => {
    expect(offeredOf(undefined, true, all)).toEqual({ state: "unreadable" });
  });

  it("is the usable items of the list once read", () => {
    expect(offeredOf([1, 2, 3], false, (items) => items.filter((n) => n > 1))).toEqual({
      state: "known",
      items: [2, 3],
    });
  });
});

describe("the features an announcement can name", () => {
  const feature = (id: string, status: Feature["status"]): Feature => ({
    id,
    description: id,
    status,
    confidence: 0.9,
    evidence: [],
  });

  it("are the live ones of the approved profile", () => {
    const profile = {
      features: [feature("a", "live"), feature("b", "unreleased"), feature("c", "unknown"), feature("d", "live")],
    } as unknown as ProductProfile;
    expect(announceableFeatures(profile).map((f) => f.id)).toEqual(["a", "d"]);
  });

  it("are none without an approved profile", () => {
    expect(announceableFeatures(null)).toEqual([]);
  });
});

describe("a refused brief", () => {
  it("puts a problem with the angle beside it", () => {
    const error = new ApiError(422, "request_invalid", "invalid", {
      angle: ["String should have at most 600 characters"],
    });
    expect(briefProblems(error).fields.angle).toEqual(["String should have at most 600 characters"]);
  });

  it("says a problem with the brief as a whole for the form", () => {
    const error = new ApiError(422, "request_invalid", "invalid", {
      body: ["Value error, an announcement names the feature it announces, and only it does"],
    });
    expect(briefProblems(error).form).toEqual([
      "Value error, an announcement names the feature it announces, and only it does",
    ]);
  });

  it("says a run that could not be started for the form", () => {
    const error = new ApiError(503, "writing_not_started", "The run could not be handed to a worker.");
    expect(briefProblems(error)).toEqual({ fields: {}, form: ["The run could not be handed to a worker."] });
  });

  it("puts a refusal of the chosen image beside the image", () => {
    for (const code of ["media_asset_not_found", "media_asset_not_usable"] as const) {
      const problems = briefProblems(new ApiError(409, code, "media asset 1 cannot go with a post: it was rejected"));
      expect(problems.form).toEqual([]);
      expect(problems.fields.asset).toHaveLength(1);
    }
  });

  it("keeps the API's reason when an image cannot go with a post", () => {
    const problems = briefProblems(new ApiError(409, "media_asset_not_usable", "it is still being made"));
    expect(problems.fields.asset?.[0]).toContain("it is still being made");
  });

  it("puts a refusal of the chosen research beside the research", () => {
    for (const code of ["research_not_found", "research_not_usable"] as const) {
      const problems = briefProblems(new ApiError(409, code, "research run 1 is failed"));
      expect(problems.form).toEqual([]);
      expect(problems.fields.research_run_id).toHaveLength(1);
    }
  });
});
