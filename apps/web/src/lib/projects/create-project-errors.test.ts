import { describe, expect, it } from "vitest";

import { ApiError, apiErrorFrom } from "@/lib/api/api-error";

import { createProjectErrors } from "./create-project-errors";

describe("errors of creating a project", () => {
  it("are none before anything failed", () => {
    expect(createProjectErrors(null)).toEqual({ name: [], repositoryUrl: [], form: [] });
  });

  it("show a refused repository URL beside its field, in the API's words", () => {
    const error = apiErrorFrom(400, { code: "invalid_repository_url", message: "only https:// URLs" });
    expect(createProjectErrors(error).repositoryUrl).toEqual(["only https:// URLs"]);
  });

  it("show a conflict beside the repository URL", () => {
    const error = apiErrorFrom(409, { code: "project_already_exists", message: "exists" });
    expect(createProjectErrors(error).repositoryUrl[0]).toContain("already exists");
  });

  it("show each validation message beside the field it names", () => {
    const error = apiErrorFrom(422, {
      detail: [
        { loc: ["body", "name"], msg: "String should have at least 1 character", type: "string_too_short" },
        { loc: ["body", "repository_url"], msg: "String should have at most 2000 characters", type: "x" },
        { loc: ["body"], msg: "Input should be a valid dictionary", type: "x" },
      ],
    });
    expect(createProjectErrors(error)).toEqual({
      name: ["String should have at least 1 character"],
      repositoryUrl: ["String should have at most 2000 characters"],
      form: ["Input should be a valid dictionary"],
    });
  });

  it("show anything else for the form as a whole", () => {
    const error = apiErrorFrom(502, { code: "api_unreachable", message: "down" });
    expect(createProjectErrors(error).form[0]).toContain("make api");
  });
});

describe("an error response", () => {
  it("is the API's problem when it has a known code", () => {
    const error = apiErrorFrom(409, { code: "analysis_already_running", message: "busy" });
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, code: "analysis_already_running", message: "busy" });
  });

  it("is unexpected when the body is not one of the known shapes", () => {
    for (const body of [undefined, null, "Bad Gateway", [], { code: "made_up", message: "x" }, { code: 1 }]) {
      expect(apiErrorFrom(500, body).code).toBe("unexpected_response");
    }
  });
});
