/** Where each failure of a settings form is shown: beside a field, or for the form as a whole. */

import type { ApiError } from "@/lib/api/api-error";
import { errorMessage } from "@/lib/api/error-message";

export type FormProblems = {
  /** What was said about each field, by the field's name in the request. */
  fields: Readonly<Record<string, readonly string[]>>;
  /** Failures that are not about one field: a conflict, an unknown name, the API being down. */
  form: readonly string[];
};

export const NO_PROBLEMS: FormProblems = { fields: {}, form: [] };

/** The API's answer to a form's request, put where the form shows it. */
export function formProblems(error: ApiError | null, fields: readonly string[]): FormProblems {
  if (error === null) return NO_PROBLEMS;
  if (error.code !== "request_invalid") return { fields: {}, form: [errorMessage(error)] };
  const byField: Record<string, readonly string[]> = {};
  const form: string[] = [];
  for (const [field, messages] of Object.entries(error.fieldErrors)) {
    if (fields.includes(field)) byField[field] = messages;
    else form.push(...messages);
  }
  // A 422 that names nothing still has to say that nothing was saved.
  if (Object.keys(byField).length === 0 && form.length === 0) form.push(errorMessage(error));
  return { fields: byField, form };
}

/** The problems of one field, never undefined. */
export function fieldProblems(problems: FormProblems, field: string): readonly string[] {
  return Object.hasOwn(problems.fields, field) ? problems.fields[field] : [];
}
