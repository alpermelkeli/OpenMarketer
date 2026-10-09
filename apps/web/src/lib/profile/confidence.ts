/**
 * How a confidence score is shown.
 *
 * The bands are a reading aid for the reviewer, not a rule: nothing is blocked or
 * allowed by them. If the product ever decides what "needs review" means, that
 * decision belongs to the API and this file should display it.
 */

export type ConfidenceLevel = "low" | "medium" | "high";

/** Below this a claim is listed under "Needs your attention". */
export const LOW_CONFIDENCE_BELOW = 0.6;
const HIGH_CONFIDENCE_FROM = 0.8;

export function confidenceLevel(confidence: number): ConfidenceLevel {
  if (confidence < LOW_CONFIDENCE_BELOW) return "low";
  return confidence < HIGH_CONFIDENCE_FROM ? "medium" : "high";
}

export function confidencePercent(confidence: number): number {
  return Math.round(Math.min(1, Math.max(0, confidence)) * 100);
}
