import { describe, expect, it } from "vitest";

import type { MediaAssetVersion } from "@/lib/api/types";

import { checkReportOf, roundsOf, verifierReportOf, versionsCost } from "./reports";

const MEASURED = { width: 1080, height: 1080, size_bytes: 421_888, mime: "image/png", safe_zone: "not measured" };

describe("the programmatic checks of a version", () => {
  it("are none until the version is reviewed", () => {
    expect(checkReportOf(null)).toBeNull();
  });

  it("say what was measured when everything held", () => {
    const report = checkReportOf({ passed: true, failures: [], copy: { passed: true, missing: [], misspelled: [] }, ...MEASURED });
    expect(report?.passed).toBe(true);
    expect(report?.items.map((item) => [item.label, item.passed, item.detail])).toEqual([
      ["Size", true, "1080 × 1080 pixels"],
      ["Format", true, "image/png"],
      ["File size", true, "412 KB"],
      ["Words", true, "Every line of the copy reads as asked."],
    ]);
  });

  it("mark what failed, with the lines of copy that do not read as asked", () => {
    const report = checkReportOf({
      passed: false,
      failures: ["size", "copy"],
      copy: { passed: false, missing: ["Try it free"], misspelled: [{ expected: "Ship faster", read: "Shlp faster" }] },
      ...MEASURED,
    });
    expect(report?.items.find((item) => item.label === "Size")).toMatchObject({ passed: false });
    expect(report?.items.find((item) => item.label === "Words")).toMatchObject({
      passed: false,
      detail: "2 lines of the copy do not read as asked.",
      lines: [
        { expected: "Try it free", read: null, verifier: null, verdict: null },
        { expected: "Ship faster", read: "Shlp faster", verifier: null, verdict: null },
      ],
    });
  });

  it("list the lines OCR confirmed, and pass the words when it confirmed every one", () => {
    const report = checkReportOf({
      passed: true,
      failures: [],
      notices: [],
      copy: { confirmed: ["Ship faster", "Try it free"], unconfirmed: [] },
      ...MEASURED,
    });
    expect(report?.items.at(-1)).toEqual({
      label: "Words",
      passed: true,
      detail: "OCR read every line of the copy as asked.",
      confirmed: ["Ship faster", "Try it free"],
    });
  });

  it("leave a line OCR could not confirm unconfirmed while the verifier has given no verdict", () => {
    const report = checkReportOf({
      passed: true,
      failures: [],
      notices: ["copy_unconfirmed"],
      copy: { confirmed: ["Ship faster"], unconfirmed: [{ expected: "Çay saati", read: "Cay saat" }] },
      ...MEASURED,
    });
    const words = report?.items.at(-1);
    expect(words).toMatchObject({
      passed: false,
      unconfirmed: true,
      detail: "OCR confirmed 1 of 2 lines of the copy; no one has confirmed the others.",
      confirmed: ["Ship faster"],
      lines: [
        {
          expected: "Çay saati",
          read: "Cay saat",
          verifier: null,
          verdict: "The verifier gave no verdict on this line, so it is not confirmed.",
        },
      ],
    });
    expect(words?.detail).not.toContain("reads as asked");
    expect(report?.otherNotices).toEqual([]);
  });

  it("show what the verifier read of each unconfirmed line and whether it matches", () => {
    const check = {
      passed: true,
      failures: [],
      notices: ["copy_unconfirmed"],
      copy: {
        confirmed: [],
        unconfirmed: [
          { expected: "Çay saati", read: "Cay saat" },
          { expected: "Try it free", read: "" },
        ],
      },
      ...MEASURED,
    };
    const matching = checkReportOf(check, {
      criteria: [],
      copy_verdict: [
        { line: 1, expected: "Çay saati", transcribed: "Çay saati", matches: true },
        { line: 2, expected: "Try it free", transcribed: "Try it free", matches: true },
      ],
    });
    expect(matching?.items.at(-1)).toMatchObject({
      passed: true,
      detail: "OCR confirmed 0 of 2 lines of the copy; the verifier read the others as asked.",
    });
    expect(matching?.items.at(-1)?.lines?.map((line) => [line.read, line.verdict])).toEqual([
      ["Cay saat", "The verifier read “Çay saati” — matches"],
      [null, "The verifier read “Try it free” — matches"],
    ]);

    const wrong = checkReportOf(check, {
      criteria: [],
      copy_verdict: [
        { line: 1, expected: "Çay saati", transcribed: "Cay saati", matches: false },
        { line: 2, expected: "Try it free", transcribed: null, matches: false },
      ],
    });
    expect(wrong?.items.at(-1)).toMatchObject({
      passed: false,
      detail: "OCR confirmed 0 of 2 lines of the copy; the verifier read 2 of the others differently from what was asked.",
    });
    expect(wrong?.items.at(-1)?.lines?.map((line) => line.verdict)).toEqual([
      "The verifier read “Cay saati” — does not match",
      "The verifier gave no reading of it — does not match",
    ]);
  });

  it("keep a notice it has no words for by its name", () => {
    const report = checkReportOf({ passed: true, failures: [], notices: ["copy_unconfirmed", "faint", 2], ...MEASURED });
    expect(report?.otherNotices).toEqual(["faint"]);
  });

  it("say when the words could not be read, and leave out a copy check the request did not ask for", () => {
    const unread = checkReportOf({ passed: false, failures: ["copy_unread"], copy: { passed: false, unread: true }, ...MEASURED });
    expect(unread?.items.at(-1)).toMatchObject({ label: "Words", passed: false });
    const noCopy = checkReportOf({ passed: true, failures: [], copy: null, ...MEASURED });
    expect(noCopy?.items.map((item) => item.label)).not.toContain("Words");
  });

  it("keep a failure it has no words for by its name, and skip fields of the wrong shape", () => {
    const report = checkReportOf({ passed: false, failures: ["contrast", "verifier:legibility", 3], width: "wide" });
    expect(report?.other).toEqual(["contrast"]);
    expect(report?.items).toEqual([]);
  });
});

describe("the verifier's report on a version", () => {
  it("is none when the verifier was not asked", () => {
    expect(verifierReportOf(null)).toBeNull();
  });

  it("lists every criterion in words, with its severity and note", () => {
    const report = verifierReportOf({
      criteria: [
        { criterion: "legibility", passed: true, severity: "none", note: "" },
        { criterion: "brand_fit", passed: false, severity: "major", note: "The palette is off." },
        { criterion: "sparkle", passed: true, severity: "minor", note: "" },
        { criterion: "composition", severity: "none" },
        "<script>",
      ],
      edit_suggestions: ["Use the brand's blue", 4],
    });
    expect(report?.criteria).toEqual([
      { label: "Legibility", passed: true, severity: "No issue", note: "" },
      { label: "Fits the brand", passed: false, severity: "Major", note: "The palette is off." },
      { label: "sparkle", passed: true, severity: "Minor", note: "" },
    ]);
    expect(report?.suggestions).toEqual(["Use the brand's blue"]);
  });
});

function version(id: string, round: number, createdAt: string, cost = 0.04): MediaAssetVersion {
  return {
    id,
    round,
    technique: "generated",
    mime: "image/png",
    width: 1080,
    height: 1080,
    size_bytes: 1000,
    provider: null,
    model: null,
    prompt: null,
    params: {},
    has_code: false,
    code_chars: 0,
    check_report: null,
    verifier_report: null,
    passed: null,
    cost_usd: cost,
    created_at: createdAt,
  };
}

describe("the rounds of a run", () => {
  it("group the versions by round, the first round first and each oldest first", () => {
    const rounds = roundsOf([
      version("c", 2, "2026-10-18T12:03:00Z"),
      version("b", 1, "2026-10-18T12:02:00Z"),
      version("a", 1, "2026-10-18T12:01:00Z"),
    ]);
    expect(rounds.map((r) => [r.round, r.versions.map((v) => v.id)])).toEqual([
      [1, ["a", "b"]],
      [2, ["c"]],
    ]);
  });

  it("add up what every version cost", () => {
    expect(versionsCost([version("a", 1, "x", 0.04), version("b", 2, "y", 0.02)])).toBeCloseTo(0.06);
  });
});
