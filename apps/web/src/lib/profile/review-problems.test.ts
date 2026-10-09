import { describe, expect, it } from "vitest";

import { apiErrorFrom } from "@/lib/api/api-error";

import { approveProblem, saveEditProblems } from "./review-problems";

describe("a failed save of an edit", () => {
  it("names the part of the profile each validation message is about", () => {
    const error = apiErrorFrom(422, {
      detail: [
        { loc: ["body", "profile", "product", "name"], msg: "String should have at least 1 character", type: "x" },
        { loc: ["body", "profile", "features", 2, "description"], msg: "Field required", type: "x" },
      ],
    });
    expect(saveEditProblems(error)).toEqual([
      "product.name: String should have at least 1 character",
      "features.2.description: Field required",
    ]);
  });

  it("falls back to general advice", () => {
    const error = apiErrorFrom(503, { code: "database_unavailable", message: "x" });
    expect(saveEditProblems(error)[0]).toContain("make up");
  });
});

describe("a failed approval", () => {
  it("says the version is already approved on 409", () => {
    const error = apiErrorFrom(409, { code: "profile_already_approved", message: "x" });
    expect(approveProblem(error)).toContain("already been approved");
  });

  it("explains a refusal of the proxy", () => {
    const error = apiErrorFrom(403, { code: "proxy_request_refused", message: "x" });
    expect(approveProblem(error)).toContain("did not come from this page");
  });
});
