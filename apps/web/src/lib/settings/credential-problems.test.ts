import { describe, expect, it } from "vitest";

import { apiErrorFrom } from "@/lib/api/api-error";

import { setCredentialProblems } from "./credential-problems";

describe("problems of storing a credential", () => {
  it("are none before anything failed", () => {
    expect(setCredentialProblems(null)).toEqual({ value: [], row: [] });
  });

  it("show a refused value beside the field, in the API's words", () => {
    const error = apiErrorFrom(422, {
      detail: [{ type: "value_error", loc: ["body", "value"], msg: "a credential cannot be empty" }],
    });
    expect(setCredentialProblems(error)).toEqual({ value: ["a credential cannot be empty"], row: [] });
  });

  it("show a refusal of the request as a whole in the row", () => {
    const error = apiErrorFrom(422, {
      detail: [{ type: "extra_forbidden", loc: ["body"], msg: "Extra inputs are not permitted" }],
    });
    expect(setCredentialProblems(error)).toEqual({ value: [], row: ["Extra inputs are not permitted"] });
  });

  it("say that the request was not valid when a refusal names nothing", () => {
    expect(setCredentialProblems(apiErrorFrom(422, { detail: [] })).row).toEqual(["The request was not valid."]);
  });

  it("show a conflict in the row, in the API's words", () => {
    for (const code of ["credential_set_by_environment", "encryption_key_missing"]) {
      const error = apiErrorFrom(409, { code, message: `the API's words for ${code}` });
      expect(setCredentialProblems(error)).toEqual({ value: [], row: [`the API's words for ${code}`] });
    }
  });

  it("show anything else in the row, with what to do about it", () => {
    const error = apiErrorFrom(502, { code: "api_unreachable", message: "down" });
    expect(setCredentialProblems(error).row[0]).toContain("make api");
  });
});
