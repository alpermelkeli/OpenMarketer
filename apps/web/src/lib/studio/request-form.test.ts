import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api/api-error";

import { EMPTY_STUDIO_REQUEST, assetRequestOf, copyLines, studioRequestProblems } from "./request-form";

describe("the request the form sends", () => {
  it("takes one line of copy per line, trimmed, without empty lines", () => {
    expect(copyLines("  Ship faster \n\n Try it free\r\n")).toEqual(["Ship faster", "Try it free"]);
  });

  it("is the goal, the copy, the platform and the notes as typed, trimmed", () => {
    expect(
      assetRequestOf({ goal: " Launch post ", copy: "Ship faster", platform: "x_post", styleNotes: " calm " }),
    ).toEqual({ goal: "Launch post", exact_copy: ["Ship faster"], platform: "x_post", style_notes: "calm" });
    expect(assetRequestOf(EMPTY_STUDIO_REQUEST).exact_copy).toEqual([]);
  });
});

describe("a refused request", () => {
  it("puts each problem beside its field, and a line's beside the copy", () => {
    const error = new ApiError(422, "request_invalid", "invalid", {
      goal: ["String should have at least 1 character"],
      "exact_copy.2": ["String should have at most 120 characters"],
      exact_copy: ["List should have at most 6 items"],
    });
    const problems = studioRequestProblems(error);
    expect(problems.fields.goal).toEqual(["String should have at least 1 character"]);
    expect(problems.fields.exact_copy).toHaveLength(2);
    expect(problems.form).toEqual([]);
  });

  it("is said for the form as a whole when it is not about a field", () => {
    const error = new ApiError(503, "studio_not_started", "The run could not be handed to the worker.");
    expect(studioRequestProblems(error).form).toEqual(["The run could not be handed to the worker."]);
  });

  it("is nothing before a request is sent", () => {
    expect(studioRequestProblems(null)).toEqual({ fields: {}, form: [] });
  });
});
