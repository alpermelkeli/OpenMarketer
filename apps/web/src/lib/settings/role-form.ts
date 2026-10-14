/**
 * A role's choice as the form holds it (text in each field) and as the API takes it.
 * Only what text cannot carry is checked here, that a number is a number; every rule
 * of a valid choice is the API's.
 */

import type { RoleChoiceRequest, RoleSetting } from "@/lib/api/types";

export type ReasoningEffortField = "" | "low" | "medium" | "high";

export type RoleForm = {
  model: string;
  /** One model per line, or separated by commas. */
  fallbacks: string;
  temperature: string;
  maxTokens: string;
  reasoningEffort: ReasoningEffortField;
};

export type ParsedRoleForm =
  | { choice: RoleChoiceRequest; problems: null }
  | { choice: null; problems: Readonly<Record<string, readonly string[]>> };

/** The form filled with what the workspace chose; a part it did not choose is left empty. */
export function roleFormOf(choice: RoleSetting["choice"]): RoleForm {
  return {
    model: choice?.model ?? "",
    fallbacks: choice?.fallbacks?.join("\n") ?? "",
    temperature: choice?.temperature?.toString() ?? "",
    maxTokens: choice?.max_tokens?.toString() ?? "",
    reasoningEffort: choice?.reasoning_effort ?? "",
  };
}

/** The request for the form, or what in it is not a number. An empty field is the file's. */
export function roleChoiceOf(form: RoleForm): ParsedRoleForm {
  const problems: Record<string, string[]> = {};
  const temperature = numberIn(form.temperature);
  if (Number.isNaN(temperature)) problems.temperature = ["Enter a number, such as 0.4, or leave it empty."];
  const maxTokens = numberIn(form.maxTokens);
  if (Number.isNaN(maxTokens) || (maxTokens !== null && !Number.isInteger(maxTokens))) {
    problems.max_tokens = ["Enter a whole number of tokens, or leave it empty."];
  }
  if (Object.keys(problems).length > 0) return { choice: null, problems };

  const fallbacks = form.fallbacks
    .split(/[\n,]/)
    .map((model) => model.trim())
    .filter((model) => model !== "");
  return {
    choice: {
      model: form.model.trim() || null,
      fallbacks: fallbacks.length > 0 ? fallbacks : null,
      temperature,
      max_tokens: maxTokens,
      reasoning_effort: form.reasoningEffort === "" ? null : form.reasoningEffort,
    },
    problems: null,
  };
}

/** `null` for an empty field, `NaN` for text that is not a number. */
export function numberIn(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const number = Number(trimmed);
  return Number.isFinite(number) ? number : Number.NaN;
}
