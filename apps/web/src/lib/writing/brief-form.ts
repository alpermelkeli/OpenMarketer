/**
 * The form that asks the writer for a post, as it holds what a person chose and typed and as the
 * API takes it. The goal decides which field is sent: an announcement names a feature, an angle
 * carries the person's words, an introduction carries neither. Every rule of a valid brief (how
 * long an angle may be, whether the feature is live) is the API's and the run's; their answer is
 * put beside the field.
 */

import type { ApiError } from "@/lib/api/api-error";
import type { Feature, ProductProfile, WritingBrief, WritingGoal } from "@/lib/api/types";
import { formProblems, type FormProblems } from "@/lib/settings/form-problems";

export type BriefForm = {
  goal: WritingGoal;
  /** The feature an announcement names; "" while none is chosen. */
  feature: string;
  angle: string;
  variants: number;
};

export const DEFAULT_VARIANTS = 3;

/** How many texts a person may ask for, as the API takes them: one to five. */
export const VARIANT_OPTIONS: readonly number[] = [1, 2, 3, 4, 5];

export const EMPTY_BRIEF: BriefForm = { goal: "introduce", feature: "", angle: "", variants: DEFAULT_VARIANTS };

/** The brief as the API takes it: only the field of the chosen goal, the angle trimmed. */
export function writingBriefOf(form: BriefForm): WritingBrief {
  const brief: WritingBrief = { platform: "x", goal: form.goal, variants: form.variants };
  if (form.goal === "announce") brief.feature = form.feature === "" ? null : form.feature;
  if (form.goal === "angle") brief.angle = form.angle.trim();
  return brief;
}

/**
 * The features an announcement can name: those the approved profile holds as live, the only ones
 * that may be mentioned publicly. Whether one still is when the run starts is the run's to check.
 */
export function announceableFeatures(profile: ProductProfile | null): Feature[] {
  return profile === null ? [] : (profile.features ?? []).filter((feature) => feature.status === "live");
}

const FIELDS = ["goal", "feature", "angle", "variants", "platform"] as const;

/** The API's refusal, beside the field it is about; one about the brief as a whole is the form's. */
export function briefProblems(error: ApiError | null): FormProblems {
  return formProblems(error, FIELDS);
}
