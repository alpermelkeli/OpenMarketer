import { describe, expect, it } from "vitest";

import type { Feature, ProductProfile } from "@/lib/api/types";

import { attentionItems, featuresInReviewOrder } from "./attention";

const evidence = [{ file: "README.md", lines: "1-3" }];
const sure = { evidence, confidence: 0.9 };

function feature(id: string, overrides: Partial<Feature> = {}): Feature {
  return { id, description: `${id} description`, status: "live", ...sure, ...overrides };
}

function profile(overrides: Partial<ProductProfile> = {}): ProductProfile {
  return {
    schema_version: 1,
    product: { name: "Memoria", type: "consumer_app", ...sure },
    features: [],
    brand: sure,
    audience: sure,
    business_model: { ...sure, type: "free" },
    measurement: sure,
    ...overrides,
  };
}

describe("what needs attention", () => {
  it("is nothing when every part is confident, evidenced and decided", () => {
    expect(attentionItems(profile({ features: [feature("sync")] }))).toEqual([]);
  });

  it("includes a feature whose status is unknown", () => {
    const items = attentionItems(profile({ features: [feature("sync", { status: "unknown" })] }));
    expect(items).toMatchObject([
      { kind: "feature", subject: "sync", reasons: ["status_unknown"], anchor: "feature-sync" },
    ]);
  });

  it("includes a section with low confidence", () => {
    const items = attentionItems(profile({ audience: { evidence, confidence: 0.5 } }));
    expect(items).toMatchObject([
      { kind: "section", subject: "audience", reasons: ["low_confidence"], confidence: 0.5 },
    ]);
  });

  it("does not include a part exactly at the threshold", () => {
    expect(attentionItems(profile({ audience: { evidence, confidence: 0.6 } }))).toEqual([]);
  });

  it("includes a part without evidence, with every reason that applies", () => {
    const items = attentionItems(profile({ brand: { evidence: [], confidence: 0 } }));
    expect(items).toMatchObject([{ subject: "brand", reasons: ["no_evidence", "low_confidence"] }]);
  });

  it("treats a section the profile leaves out as unevidenced", () => {
    const withoutMeasurement = profile({ measurement: undefined });
    expect(attentionItems(withoutMeasurement)).toMatchObject([{ subject: "measurement" }]);
  });

  it("puts undecided status first, then missing evidence, then the least confident", () => {
    const items = attentionItems(
      profile({
        features: [
          feature("low", { confidence: 0.4 }),
          feature("lower", { confidence: 0.2 }),
          feature("undecided", { status: "unknown" }),
        ],
        brand: { evidence: [], confidence: 0.9 },
      }),
    );
    expect(items.map((item) => item.subject)).toEqual(["undecided", "brand", "lower", "low"]);
  });
});

describe("features in review order", () => {
  it("lists those needing attention first and keeps the profile's order otherwise", () => {
    const ordered = featuresInReviewOrder([
      feature("a"),
      feature("b", { status: "unknown" }),
      feature("c"),
      feature("d", { confidence: 0.1 }),
    ]);
    expect(ordered.map((f) => f.id)).toEqual(["b", "d", "a", "c"]);
  });
});
