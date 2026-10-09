/**
 * Which parts of a profile the reviewer should look at first, and why.
 *
 * This orders what is shown; it decides nothing. Every part stays visible and
 * editable whether or not it is listed here.
 */

import type { Feature, ProductProfile } from "@/lib/api/types";

import { LOW_CONFIDENCE_BELOW } from "./confidence";

export type SectionKey = "product" | "brand" | "audience" | "business_model" | "measurement";

export type AttentionReason = "status_unknown" | "no_evidence" | "low_confidence";

export type AttentionItem = {
  /** The id of the element on the page that shows this part. */
  anchor: string;
  kind: "feature" | "section";
  /** A feature id or a section key: text from the profile, to be shown as text. */
  subject: string;
  reasons: AttentionReason[];
  confidence: number;
};

export const SECTION_KEYS: readonly SectionKey[] = [
  "product",
  "brand",
  "audience",
  "business_model",
  "measurement",
];

export function sectionAnchor(section: SectionKey): string {
  return `section-${section}`;
}

export function featureAnchor(featureId: string): string {
  return `feature-${featureId}`;
}

type Claim = { evidence?: readonly unknown[]; confidence?: number };

function reasonsFor(claim: Claim, statusUnknown: boolean): AttentionReason[] {
  const reasons: AttentionReason[] = [];
  if (statusUnknown) reasons.push("status_unknown");
  if ((claim.evidence ?? []).length === 0) reasons.push("no_evidence");
  if ((claim.confidence ?? 0) < LOW_CONFIDENCE_BELOW) reasons.push("low_confidence");
  return reasons;
}

export function featureNeedsAttention(feature: Feature): boolean {
  return reasonsFor(feature, feature.status === "unknown").length > 0;
}

/**
 * The parts to decide on, most uncertain first: undecided feature status before
 * anything else, then missing evidence, then by ascending confidence.
 */
export function attentionItems(profile: ProductProfile): AttentionItem[] {
  const items: AttentionItem[] = [];

  for (const feature of profile.features ?? []) {
    const reasons = reasonsFor(feature, feature.status === "unknown");
    if (reasons.length === 0) continue;
    items.push({
      anchor: featureAnchor(feature.id),
      kind: "feature",
      subject: feature.id,
      reasons,
      confidence: feature.confidence ?? 0,
    });
  }

  for (const key of SECTION_KEYS) {
    const section: Claim = profile[key] ?? {};
    const reasons = reasonsFor(section, false);
    if (reasons.length === 0) continue;
    items.push({
      anchor: sectionAnchor(key),
      kind: "section",
      subject: key,
      reasons,
      confidence: section.confidence ?? 0,
    });
  }

  return items.sort(byUncertainty);
}

/** Features in review order: those needing attention first, otherwise as the profile lists them. */
export function featuresInReviewOrder(features: readonly Feature[]): Feature[] {
  const flagged = features.filter(featureNeedsAttention);
  const settled = features.filter((feature) => !featureNeedsAttention(feature));
  return [...flagged, ...settled];
}

const REASON_RANK: Record<AttentionReason, number> = {
  status_unknown: 0,
  no_evidence: 1,
  low_confidence: 2,
};

function byUncertainty(a: AttentionItem, b: AttentionItem): number {
  const rank = REASON_RANK[a.reasons[0]] - REASON_RANK[b.reasons[0]];
  return rank !== 0 ? rank : a.confidence - b.confidence;
}
