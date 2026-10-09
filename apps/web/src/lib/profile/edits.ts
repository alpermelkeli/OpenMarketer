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

/** How many values differ between the stored profile and the working copy. */
export function countChanges(stored: ProductProfile, edited: ProductProfile): number {
  return changedParts(stored, edited).length;
}

/**
 * What differs between two profiles, one line per difference, for a reader: the
 * values the dashboard can edit by name, anything else by the part it is in. Used
 * to say what an edit changed; it is a list, not a diff of the texts.
 */
export function changedParts(before: ProductProfile, after: ProductProfile): string[] {
  const changes: string[] = [];
  const add = (differs: boolean, label: string) => {
    if (differs) changes.push(label);
  };

  add(before.product.name !== after.product.name, "Product name");
  add(differs(before.product, after.product, ["name"]), "Product: other values");

  const afterFeatures = new Map((after.features ?? []).map((feature) => [feature.id, feature]));
  const beforeIds = new Set((before.features ?? []).map((feature) => feature.id));
  for (const feature of before.features ?? []) {
    const other = afterFeatures.get(feature.id);
    if (other === undefined) {
      changes.push(`Feature removed: ${feature.id}`);
      continue;
    }
    add(feature.status !== other.status, `Status of ${feature.id}`);
    add(feature.description !== other.description, `Description of ${feature.id}`);
    add(differs(feature, other, ["status", "description"]), `Evidence or confidence of ${feature.id}`);
  }
  for (const feature of after.features ?? []) {
    add(!beforeIds.has(feature.id), `Feature added: ${feature.id}`);
  }

  add((before.brand?.voice ?? null) !== (after.brand?.voice ?? null), "Brand voice");
  add(differs(before.brand, after.brand, ["voice"]), "Brand: other values");
  add((before.audience?.primary ?? null) !== (after.audience?.primary ?? null), "Primary audience");
  add(differs(before.audience, after.audience, ["primary"]), "Audience: other values");
  add(before.business_model?.type !== after.business_model?.type, "Business model type");
  add(differs(before.business_model, after.business_model, ["type"]), "Business model: other values");
  add(differs(before.measurement, after.measurement, []), "Measurement");
  return changes;
}

/**
 * Whether two parts differ outside the named values. Both come from the same API, which
 * writes a part's values in one order, so comparing their JSON is enough for display.
 */
function differs(before: object | undefined, after: object | undefined, named: readonly string[]): boolean {
  const rest = (part: object | undefined) =>
    JSON.stringify(Object.entries(part ?? {}).filter(([key]) => !named.includes(key)));
  return rest(before) !== rest(after);
}

function textOrNone(text: string): string | null {
  return text.trim() === "" ? null : text;
}
