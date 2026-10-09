/** Words for the profile's enumerated values and open slugs. Display only. */

import type { BusinessModelType, FeatureStatus } from "@/lib/api/types";
import type { Tone } from "@/lib/analysis/run-status";

type StatusDisplay = { label: string; tone: Tone; meaning: string };

const FEATURE_STATUS: Record<FeatureStatus, StatusDisplay> = {
  live: { label: "Live", tone: "success", meaning: "Released. May be mentioned publicly." },
  unreleased: {
    label: "Unreleased",
    tone: "neutral",
    meaning: "In the code but not released. Kept out of public content.",
  },
  unknown: {
    label: "Unknown",
    tone: "warning",
    meaning: "The analyzer could not tell. Kept out of public content until you decide.",
  },
};

export const FEATURE_STATUSES = Object.keys(FEATURE_STATUS) as FeatureStatus[];

export function featureStatusDisplay(status: FeatureStatus): StatusDisplay {
  return FEATURE_STATUS[status];
}

const BUSINESS_MODEL: Record<BusinessModelType, string> = {
  free: "Free",
  subscription: "Subscription",
  one_time_purchase: "One-time purchase",
  ads: "Ads",
  unknown: "Unknown",
};

export const BUSINESS_MODEL_TYPES = Object.keys(BUSINESS_MODEL) as BusinessModelType[];

export function businessModelLabel(type: BusinessModelType): string {
  return BUSINESS_MODEL[type];
}

/** "consumer_app" and "create-memory-rooms" as words: "Consumer app", "Create memory rooms". */
export function humanizeSlug(slug: string): string {
  const words = slug.replace(/[_-]+/g, " ").trim();
  return words === "" ? slug : words.charAt(0).toUpperCase() + words.slice(1);
}
