/**
 * The form that asks the studio for an image, as it holds what a person typed and as the API
 * takes it. The words are passed on as typed, trimmed; every rule of a valid request (how long
 * a goal may be, how many lines of copy) is the API's, and its answer is put beside the field.
 */

import type { ApiError } from "@/lib/api/api-error";
import type { AssetRequest, Platform } from "@/lib/api/types";
import { formProblems, type FormProblems } from "@/lib/settings/form-problems";

export type StudioRequestForm = {
  goal: string;
  /** One line of exact copy per line of the field. */
  copy: string;
  platform: Platform;
  styleNotes: string;
};

export const EMPTY_STUDIO_REQUEST: StudioRequestForm = {
  goal: "",
  copy: "",
  platform: "instagram_post",
  styleNotes: "",
};

/** The lines of copy the image must show, each trimmed, with empty lines left out. */
export function copyLines(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

export function assetRequestOf(form: StudioRequestForm): AssetRequest {
  return {
    goal: form.goal.trim(),
    exact_copy: copyLines(form.copy),
    platform: form.platform,
    style_notes: form.styleNotes.trim(),
  };
}

const FIELDS = ["goal", "exact_copy", "platform", "style_notes"] as const;

/** The API's refusal, beside the field it is about; a problem with one line of copy is the copy's. */
export function studioRequestProblems(error: ApiError | null): FormProblems {
  const problems = formProblems(error, [...FIELDS, ...lineFields(error)]);
  const fields: Record<string, readonly string[]> = {};
  for (const [field, messages] of Object.entries(problems.fields)) {
    const name = field.startsWith("exact_copy.") ? "exact_copy" : field;
    fields[name] = [...(fields[name] ?? []), ...messages];
  }
  return { fields, form: problems.form };
}

function lineFields(error: ApiError | null): string[] {
  if (error === null) return [];
  return Object.keys(error.fieldErrors).filter((field) => /^exact_copy\.\d+$/.test(field));
}
