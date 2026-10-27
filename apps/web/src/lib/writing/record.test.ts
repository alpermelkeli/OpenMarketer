import { describe, expect, it } from "vitest";

import { alternativesOf, checkReportOf, claimsOf, planOf, postLengthOf, reviewOf, variantLabel } from "./record";

describe("the code-rule report", () => {
  it("names each broken rule in words, with what the check found", () => {
    const report = checkReportOf({
      passed: false,
      failures: [
        { rule: "length", finding: "the text counts 301, over 280" },
        { rule: "no_links", finding: "the text holds a link (example.com)" },
      ],
      length: 301,
      flagged_numbers: ["40"],
    });
    expect(report).toEqual({
      passed: false,
      length: 301,
      failures: [
        { rule: "length", label: "Length on X", finding: "the text counts 301, over 280" },
        { rule: "no_links", label: "No links", finding: "the text holds a link (example.com)" },
      ],
      flaggedNumbers: ["40"],
    });
  });

  it("shows a rule it has no words for by the name the run gave it", () => {
    const report = checkReportOf({ passed: false, failures: [{ rule: "emoji", finding: "too many" }] });
    expect(report?.failures[0].label).toBe("emoji");
  });

  it("leaves out a finding without its shape, and says nothing it was not told", () => {
    const report = checkReportOf({ failures: [{ rule: 3 }, "x", null], length: -1, flagged_numbers: [7, "12"] });
    expect(report).toEqual({ passed: null, length: null, failures: [], flaggedNumbers: ["12"] });
  });

  it("is none when there is no report", () => {
    expect(checkReportOf(null)).toBeNull();
    expect(checkReportOf([])).toBeNull();
  });
});

describe("the post's length on X", () => {
  it("is the check's count, kept when the check found nothing against it", () => {
    expect(postLengthOf(checkReportOf({ passed: true, failures: [], length: 212 }))).toEqual({
      count: 212,
      kept: true,
    });
  });

  it("is over the limit when the length rule failed", () => {
    const report = checkReportOf({ passed: false, failures: [{ rule: "length", finding: "301, over 280" }], length: 301 });
    expect(postLengthOf(report)).toEqual({ count: 301, kept: false });
  });

  it("is kept when only another rule failed, and unknown when the check did not say", () => {
    const other = checkReportOf({ passed: false, failures: [{ rule: "no_links", finding: "a link" }], length: 90 });
    expect(postLengthOf(other)?.kept).toBe(true);
    expect(postLengthOf(checkReportOf({ length: 90 }))?.kept).toBeNull();
  });

  it("is none without a measured length", () => {
    expect(postLengthOf(checkReportOf({ passed: true }))).toBeNull();
    expect(postLengthOf(null)).toBeNull();
  });
});

describe("the critic's review", () => {
  it("gives each criterion in words with its severity and note", () => {
    const review = reviewOf({
      variant: 0,
      criteria: [
        { criterion: "facts", passed: true, severity: "none", note: "" },
        { criterion: "brand_voice", passed: false, severity: "minor", note: "a little stiff" },
      ],
      risky_claim: false,
      risky_note: "",
      suggestions: ["Shorter opening"],
    });
    expect(review).toEqual({
      criteria: [
        { label: "Facts", passed: true, severity: "No issue", minor: false, note: "" },
        { label: "The brand's voice", passed: false, severity: "Minor", minor: true, note: "a little stiff" },
      ],
      riskyClaim: false,
      riskyNote: "",
      suggestions: ["Shorter opening"],
    });
  });

  it("leaves out a verdict without a criterion or a pass, and keeps an unknown one by its name", () => {
    const review = reviewOf({
      criteria: [{ criterion: "facts" }, { passed: true }, { criterion: "humour", passed: false, severity: "grave" }],
    });
    expect(review?.criteria).toEqual([{ label: "humour", passed: false, severity: "grave", minor: false, note: "" }]);
    expect(review?.riskyClaim).toBeNull();
  });

  it("is none when the critic did not review", () => {
    expect(reviewOf(null)).toBeNull();
    expect(reviewOf("passed")).toBeNull();
  });
});

describe("the claims", () => {
  it("carry the features they rest on and where each was found", () => {
    expect(
      claimsOf([
        {
          statement: "Exports to CSV",
          feature_ids: ["csv-export"],
          evidence: [
            { feature_id: "csv-export", file: "src/export.ts", lines: "10-24" },
            { feature_id: "csv-export", file: "README.md", lines: null },
          ],
        },
      ]),
    ).toEqual([
      {
        statement: "Exports to CSV",
        featureIds: ["csv-export"],
        evidence: [
          { featureId: "csv-export", file: "src/export.ts", lines: "10-24" },
          { featureId: "csv-export", file: "README.md", lines: null },
        ],
      },
    ]);
  });

  it("leave out a claim without a statement and evidence without a file", () => {
    expect(claimsOf([{ feature_ids: ["a"] }, { statement: "Fast", evidence: [{ lines: "1" }, { file: "" }] }])).toEqual([
      { statement: "Fast", featureIds: [], evidence: [] },
    ]);
  });
});

describe("the plan", () => {
  it("is the angle, the format in words and the features it rests on", () => {
    expect(planOf({ angle: "Less busywork", format: "tip", feature_ids: ["csv-export"] })).toEqual({
      angle: "Less busywork",
      format: "A practical tip",
      featureIds: ["csv-export"],
    });
  });

  it("keeps a format it has no words for by its name, and says nothing of what is missing", () => {
    expect(planOf({ format: "thread" })).toEqual({ angle: null, format: "thread", featureIds: [] });
    expect(planOf({})).toEqual({ angle: null, format: null, featureIds: [] });
  });
});

describe("the other variants", () => {
  const variant = (text: string) => ({
    text,
    angle: "speed",
    language: "en",
    claims: [{ statement: "Fast", feature_ids: ["f"] }],
  });

  it("are in the order of the round, each with its checks and review", () => {
    const alternatives = alternativesOf([
      { index: 2, variant: variant("Third"), check: { passed: true, failures: [], length: 5 }, review: null },
      { index: 0, variant: variant("First"), check: null, review: { criteria: [], risky_claim: true } },
    ]);
    expect(alternatives.map((a) => a.text)).toEqual(["First", "Third"]);
    expect(alternatives[0].review?.riskyClaim).toBe(true);
    expect(alternatives[0].check).toBeNull();
    expect(alternatives[1].check?.length).toBe(5);
    expect(alternatives[1].review).toBeNull();
    expect(alternatives[1].claims).toEqual([{ statement: "Fast", featureIds: ["f"] }]);
  });

  it("leave out an entry without an index or a text", () => {
    expect(alternativesOf([{ variant: variant("No index") }, { index: 1, variant: {} }, { index: 1 }])).toEqual([]);
  });

  it("are counted from one for a person", () => {
    expect(variantLabel(0)).toBe("Variant 1");
  });
});
