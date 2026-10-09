/**
 * Changing a profile in the review screen's working copy.
 *
 * Each function returns a new profile and leaves its input alone. Nothing here
 * reaches the server: an edit is saved by the reviewer, as a new draft version.
 * What a valid profile is stays the API's decision; these functions only keep
 * the shape (an emptied optional text becomes "no value", not an empty string).
 * A section the profile does not have is not invented: such an edit changes nothing.
 */

import type { BusinessModelType, Feature, ProductProfile } from "@/lib/api/types";

export function withProductName(profile: ProductProfile, name: string): ProductProfile {
  return { ...profile, product: { ...profile.product, name } };
}

export function withFeature(
  profile: ProductProfile,
  featureId: string,
  change: Partial<Pick<Feature, "description" | "status">>,
): ProductProfile {
  if (profile.features === undefined) return profile;
  return {
    ...profile,
    features: profile.features.map((feature) => (feature.id === featureId ? { ...feature, ...change } : feature)),
  };
}

export function withBrandVoice(profile: ProductProfile, voice: string): ProductProfile {
  if (profile.brand === undefined) return profile;
  return { ...profile, brand: { ...profile.brand, voice: textOrNone(voice) } };
}

export function withAudiencePrimary(profile: ProductProfile, primary: string): ProductProfile {
  if (profile.audience === undefined) return profile;
  return { ...profile, audience: { ...profile.audience, primary: textOrNone(primary) } };
}

export function withBusinessModelType(profile: ProductProfile, type: BusinessModelType): ProductProfile {
  if (profile.business_model === undefined) return profile;
  return { ...profile, business_model: { ...profile.business_model, type } };
}

/** How many of the editable values differ between the stored profile and the working copy. */
export function countChanges(stored: ProductProfile, edited: ProductProfile): number {
  const editedFeatures = new Map((edited.features ?? []).map((feature) => [feature.id, feature]));
  const featureChanges = (stored.features ?? []).flatMap((feature) => {
    const other = editedFeatures.get(feature.id);
    return [feature.status !== other?.status, feature.description !== other?.description];
  });
  return [
    stored.product.name !== edited.product.name,
    (stored.brand?.voice ?? null) !== (edited.brand?.voice ?? null),
    (stored.audience?.primary ?? null) !== (edited.audience?.primary ?? null),
    stored.business_model?.type !== edited.business_model?.type,
    ...featureChanges,
  ].filter(Boolean).length;
}

function textOrNone(text: string): string | null {
  return text.trim() === "" ? null : text;
}
