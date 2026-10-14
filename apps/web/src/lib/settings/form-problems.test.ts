import { describe, expect, it } from "vitest";

import { apiErrorFrom } from "@/lib/api/api-error";

import { fieldProblems, formProblems } from "./form-problems";

describe("problems of a settings form", () => {
  it("are none before anything failed", () => {
    expect(formProblems(null, ["model"])).toEqual({ fields: {}, form: [] });
  });

  it("show a refused field beside it, in the API's words", () => {
    const error = apiErrorFrom(422, {
      detail: [{ type: "value_error", loc: ["body", "temperature"], msg: "the temperature is a number from 0 to 2" }],
    });
    const problems = formProblems(error, ["model", "temperature"]);
    expect(fieldProblems(problems, "temperature")).toEqual(["the temperature is a number from 0 to 2"]);
    expect(fieldProblems(problems, "model")).toEqual([]);
    expect(problems.form).toEqual([]);
  });

  it("show a refusal of a field the form does not have for the form as a whole", () => {
    const error = apiErrorFrom(422, {
      detail: [{ type: "extra_forbidden", loc: ["body", "api_key"], msg: "Extra inputs are not permitted" }],
    });
    expect(formProblems(error, ["model"]).form).toEqual(["Extra inputs are not permitted"]);
  });

  it("show a conflict for the form as a whole", () => {
    const error = apiErrorFrom(409, {
      code: "model_set_by_environment",
      message: "the environment variable LLM_MODEL_FAST sets this model",
    });
    expect(formProblems(error, ["model"])).toEqual({
      fields: {},
      form: ["the environment variable LLM_MODEL_FAST sets this model"],
    });
  });

  it("do not find a field through an inherited name", () => {
    expect(fieldProblems(formProblems(null, []), "constructor")).toEqual([]);
  });
});
