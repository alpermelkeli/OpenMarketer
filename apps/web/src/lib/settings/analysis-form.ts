/** The limits of an analysis as the form holds them, and as the API takes them. */

import type { AnalysisLimitsRequest, AnalysisSettings } from "@/lib/api/types";

import { numberIn } from "./role-form";

export type AnalysisForm = { maxSteps: string; maxCostUsd: string };

export type ParsedAnalysisForm =
  | { limits: AnalysisLimitsRequest; problems: null }
  | { limits: null; problems: Readonly<Record<string, readonly string[]>> };

/** The form filled with what the workspace chose; a limit it did not choose is left empty. */
export function analysisFormOf(chosen: AnalysisSettings["chosen"]): AnalysisForm {
  return {
    maxSteps: chosen?.max_steps?.toString() ?? "",
    maxCostUsd: chosen?.max_cost_usd?.toString() ?? "",
  };
}

/** The request for the form, or what in it is not a number. An empty field keeps the default. */
export function analysisLimitsOf(form: AnalysisForm): ParsedAnalysisForm {
  const problems: Record<string, string[]> = {};
  const maxSteps = numberIn(form.maxSteps);
  if (Number.isNaN(maxSteps) || (maxSteps !== null && !Number.isInteger(maxSteps))) {
    problems.max_steps = ["Enter a whole number of steps, or leave it empty."];
  }
  const maxCostUsd = numberIn(form.maxCostUsd.replace(/^\$/, ""));
  if (Number.isNaN(maxCostUsd)) problems.max_cost_usd = ["Enter an amount in US dollars, such as 0.50, or leave it empty."];
  if (Object.keys(problems).length > 0) return { limits: null, problems };
  return { limits: { max_steps: maxSteps, max_cost_usd: maxCostUsd }, problems: null };
}
