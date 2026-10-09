import { describe, expect, it } from "vitest";

import type { ProductProfile } from "@/lib/api/types";

import {
  countChanges,
  withAudiencePrimary,
  withBrandVoice,
  withBusinessModelType,
  withFeature,
  withProductName,
} from "./edits";

const claim = { evidence: [{ file: "README.md", lines: "1" }], confidence: 0.9 };

const stored: ProductProfile = {
  schema_version: 1,
  product: { ...claim, name: "Memoria", type: "consumer_app", platforms: ["ios"], languages: [] },
  features: [
    { ...claim, id: "rooms", description: "Shared rooms", status: "live" },
    { ...claim, id: "sync", description: "Device sync", status: "unknown" },
  ],
  brand: { ...claim, voice: "Warm", palette: [], do_not_mention: [] },
  audience: { ...claim, primary: null, pains: [] },
  business_model: { ...claim, type: "unknown", trial_days: null },
  measurement: { ...claim, analytics: [], attribution: null, deep_links: null },
};

describe("editing a profile", () => {
  it("changes one feature and leaves the stored profile as it was", () => {
    const edited = withFeature(stored, "sync", { status: "live" });
    expect(edited.features?.map((f) => f.status)).toEqual(["live", "live"]);
    expect(stored.features?.[1].status).toBe("unknown");
    expect(edited.features?.[0]).toBe(stored.features?.[0]);
  });

  it("keeps evidence and confidence of what it changes", () => {
    const edited = withProductName(withFeature(stored, "rooms", { description: "Private rooms" }), "Memoria 2");
    expect(edited.features?.[0]).toMatchObject({ ...claim, description: "Private rooms" });
    expect(edited.product).toMatchObject({ ...claim, name: "Memoria 2", platforms: ["ios"] });
  });

  it("does not invent a section the profile does not have", () => {
    const withoutBrand: ProductProfile = { ...stored, brand: undefined };
    expect(withBrandVoice(withoutBrand, "Warm")).toBe(withoutBrand);
  });

  it("stores an emptied optional text as no value", () => {
    expect(withBrandVoice(stored, "   ").brand?.voice).toBeNull();
    expect(withAudiencePrimary(stored, "").audience?.primary).toBeNull();
    expect(withAudiencePrimary(stored, "Families").audience?.primary).toBe("Families");
  });
});

describe("counting changes", () => {
  it("is zero for an untouched copy", () => {
    expect(countChanges(stored, structuredClone(stored))).toBe(0);
  });

  it("counts each changed value once", () => {
    let edited = withFeature(stored, "sync", { status: "unreleased", description: "Sync over Wi-Fi" });
    edited = withBusinessModelType(withBrandVoice(edited, "Calm"), "free");
    expect(countChanges(stored, edited)).toBe(4);
  });

  it("is zero again when a value is put back", () => {
    const edited = withProductName(withProductName(stored, "X"), "Memoria");
    expect(countChanges(stored, edited)).toBe(0);
  });

  it("does not count clearing a text that had no value", () => {
    expect(countChanges(stored, withAudiencePrimary(stored, ""))).toBe(0);
  });
});
