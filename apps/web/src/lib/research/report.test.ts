import { describe, expect, it } from "vitest";

import type { ResearchReport } from "@/lib/api/types";

import { findingsOf, questionOf, type Finding } from "./report";

const QUESTIONS = ["What is it?", "What does it cost?"];

function report(findings: ResearchReport["findings"]): ResearchReport {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    project_id: "22222222-2222-4222-8222-222222222222",
    run_id: "33333333-3333-4333-8333-333333333333",
    requested_by: "person",
    questions: QUESTIONS,
    findings,
    notes: [],
    cost_usd: 0.01,
    model: "vendor/model",
    created_at: "2026-10-14T12:00:00Z",
  };
}

function aFinding(overrides: { [key: string]: unknown } = {}) {
  return {
    question_index: 0,
    statement: "It is a product.",
    confidence: 0.8,
    flagged: false,
    sources: [{ page_id: "p", url: "https://example.com/a", quotes: ["We ship weekly."] }],
    ...overrides,
  };
}

function only(findings: Finding[]): Finding {
  const [finding] = findings;
  if (finding === undefined) throw new Error("expected one finding");
  return finding;
}

describe("a report's findings as a screen reads them", () => {
  it("keeps a finding with its statement, confidence and sources", () => {
    expect(findingsOf(report([aFinding()]))).toEqual([
      {
        questionIndex: 0,
        statement: "It is a product.",
        confidence: 0.8,
        flagged: false,
        sources: [{ url: "https://example.com/a", quotes: ["We ship weekly."] }],
      },
    ]);
  });

  it("leaves out an entry that is not a finding", () => {
    expect(findingsOf(report([{}, { statement: 5 }, aFinding()]))).toHaveLength(1);
  });

  it("keeps an untied finding, whose index is -1", () => {
    const finding = only(findingsOf(report([aFinding({ question_index: -1 })])));
    expect(finding.questionIndex).toBe(-1);
    expect(questionOf(finding, QUESTIONS)).toBeNull();
  });

  it("is flagged only when the report says so", () => {
    expect(only(findingsOf(report([aFinding({ flagged: true })]))).flagged).toBe(true);
    expect(only(findingsOf(report([aFinding({ flagged: undefined })]))).flagged).toBe(false);
  });

  it("keeps a source only when it has a URL, and its quotes only when they are text", () => {
    const sources = [
      { page_id: "p", url: 5, quotes: ["lost"] },
      { page_id: "q", url: "https://example.com/b", quotes: ["kept", 7, null] },
    ];
    expect(only(findingsOf(report([aFinding({ sources })]))).sources).toEqual([
      { url: "https://example.com/b", quotes: ["kept"] },
    ]);
  });
});

describe("the question a finding answers", () => {
  it("is the question of the report at its index", () => {
    const finding = only(findingsOf(report([aFinding({ question_index: 1 })])));
    expect(questionOf(finding, QUESTIONS)).toBe("What does it cost?");
  });

  it("is none for an index the report's questions do not have", () => {
    const finding = only(findingsOf(report([aFinding({ question_index: 7 })])));
    expect(questionOf(finding, QUESTIONS)).toBeNull();
  });
});
