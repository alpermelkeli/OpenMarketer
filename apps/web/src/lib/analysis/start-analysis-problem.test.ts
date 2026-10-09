import { describe, expect, it } from "vitest";

import { apiErrorFrom } from "@/lib/api/api-error";

import { startAnalysisProblem } from "./start-analysis-problem";

describe("an analysis that could not be started", () => {
  it("says a run is already in progress on 409", () => {
    const problem = startAnalysisProblem(apiErrorFrom(409, { code: "analysis_already_running", message: "x" }));
    expect(problem.title).toContain("already running");
  });

  it("says how to get a worker on 503", () => {
    const problem = startAnalysisProblem(apiErrorFrom(503, { code: "analysis_not_started", message: "x" }));
    expect(problem.title).toContain("worker");
    expect(problem.text).toContain("make worker");
  });

  it("passes on the API's reason for a repository it will not clone", () => {
    const problem = startAnalysisProblem(
      apiErrorFrom(400, { code: "invalid_repository_url", message: "only https:// URLs are analysed" }),
    );
    expect(problem.text).toBe("only https:// URLs are analysed");
  });

  it("falls back to general advice", () => {
    const problem = startAnalysisProblem(apiErrorFrom(502, { code: "api_unreachable", message: "x" }));
    expect(problem.text).toContain("make api");
  });
});
