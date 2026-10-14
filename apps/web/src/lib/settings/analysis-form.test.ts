import { describe, expect, it } from "vitest";

import { analysisFormOf, analysisLimitsOf } from "./analysis-form";

describe("the form of an analysis's limits", () => {
  it("holds what the workspace chose and leaves the rest empty", () => {
    expect(analysisFormOf(null)).toEqual({ maxSteps: "", maxCostUsd: "" });
    expect(analysisFormOf({ max_steps: 60, max_cost_usd: null })).toEqual({ maxSteps: "60", maxCostUsd: "" });
  });

  it("sends the limits as numbers, an empty one as the default's", () => {
    expect(analysisLimitsOf({ maxSteps: "60", maxCostUsd: "$1.25" })).toEqual({
      limits: { max_steps: 60, max_cost_usd: 1.25 },
      problems: null,
    });
    expect(analysisLimitsOf({ maxSteps: "", maxCostUsd: "" }).limits).toEqual({ max_steps: null, max_cost_usd: null });
  });

  it("is not sent while a limit is not a number of its kind", () => {
    const parsed = analysisLimitsOf({ maxSteps: "40.5", maxCostUsd: "cheap" });
    expect(parsed.limits).toBeNull();
    expect(Object.keys(parsed.problems ?? {})).toEqual(["max_steps", "max_cost_usd"]);
  });
});
