import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api/api-error";
import type { Feature, ProductProfile } from "@/lib/api/types";

import { EMPTY_BRIEF, VARIANT_OPTIONS, announceableFeatures, briefProblems, writingBriefOf } from "./brief-form";

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
});
