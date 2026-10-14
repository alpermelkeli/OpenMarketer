import { describe, expect, it } from "vitest";

import { numberIn, roleChoiceOf, roleFormOf } from "./role-form";

const EMPTY = { model: "", fallbacks: "", temperature: "", maxTokens: "", reasoningEffort: "" } as const;

describe("a role's form", () => {
  it("is empty for a role the workspace made no choice for", () => {
    expect(roleFormOf(null)).toEqual(EMPTY);
  });

  it("holds what the workspace chose, a fallback per line", () => {
    const form = roleFormOf({
      model: "openai/gpt-9",
      fallbacks: ["openai/gpt-8", "openai/gpt-7"],
      temperature: 0.3,
      max_tokens: 2048,
      reasoning_effort: "high",
    });
    expect(form).toEqual({
      model: "openai/gpt-9",
      fallbacks: "openai/gpt-8\nopenai/gpt-7",
      temperature: "0.3",
      maxTokens: "2048",
      reasoningEffort: "high",
    });
  });
});

describe("the choice a role's form sends", () => {
  it("leaves every empty field to the configuration file", () => {
    expect(roleChoiceOf(EMPTY)).toEqual({
      choice: { model: null, fallbacks: null, temperature: null, max_tokens: null, reasoning_effort: null },
      problems: null,
    });
  });

  it("trims the model and reads fallbacks separated by lines or commas", () => {
    const parsed = roleChoiceOf({ ...EMPTY, model: " openai/gpt-9 ", fallbacks: "a/b, c/d\n\ne/f " });
    expect(parsed.choice).toMatchObject({ model: "openai/gpt-9", fallbacks: ["a/b", "c/d", "e/f"] });
  });

  it("sends numbers as numbers", () => {
    const parsed = roleChoiceOf({ ...EMPTY, temperature: "0.7", maxTokens: "512", reasoningEffort: "low" });
    expect(parsed.choice).toMatchObject({ temperature: 0.7, max_tokens: 512, reasoning_effort: "low" });
  });

  it("is not sent while a number field holds something else", () => {
    const parsed = roleChoiceOf({ ...EMPTY, temperature: "warm", maxTokens: "1.5" });
    expect(parsed.choice).toBeNull();
    expect(Object.keys(parsed.problems ?? {})).toEqual(["temperature", "max_tokens"]);
  });
});

describe("a number typed in a field", () => {
  it("is null when the field is empty and NaN when it is not a number", () => {
    expect(numberIn("  ")).toBeNull();
    expect(numberIn(" 2.5 ")).toBe(2.5);
    expect(numberIn("Infinity")).toBeNaN();
    expect(numberIn("two")).toBeNaN();
  });
});
