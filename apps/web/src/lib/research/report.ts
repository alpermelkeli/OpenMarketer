/**
 * A research report's findings as a screen shows them: a statement, its confidence, whether
 * every page it rests on raised an injection flag, and the sources it was checked against,
 * each with the quotes found in its page.
 *
 * Findings arrive from the API as plain data, so each is read defensively and kept only
 * when it has the shape the research run wrote: a finding or a source that does not is
 * left out rather than shown half-way. Display only; nothing here decides anything.
 */

import type { ResearchReport } from "@/lib/api/types";

export type FindingSource = {
  url: string;
  /** The exact phrases of the page the finding quotes. */
  quotes: string[];
};

export type Finding = {
  questionIndex: number; // -1: it answered no question of its run
  statement: string;
  confidence: number;
  /** Every page it rests on showed signs of an attempt to instruct a model. */
  flagged: boolean;
  sources: FindingSource[];
};

export function findingsOf(report: ResearchReport): Finding[] {
  return report.findings.flatMap(findingOf);
}

/** The question a finding answers, or null when it answers none of the run's. */
export function questionOf(finding: Finding, questions: readonly string[]): string | null {
  return finding.questionIndex >= 0 && finding.questionIndex < questions.length
    ? questions[finding.questionIndex]
    : null;
}

function findingOf(entry: { [key: string]: unknown }): Finding[] {
  const { statement, confidence, question_index: questionIndex, flagged, sources } = entry;
  if (typeof statement !== "string" || typeof confidence !== "number") return [];
  if (typeof questionIndex !== "number" || !Number.isInteger(questionIndex)) return [];
  return [
    {
      questionIndex,
      statement,
      confidence,
      flagged: flagged === true,
      sources: sourcesOf(sources),
    },
  ];
}

function sourcesOf(value: unknown): FindingSource[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap(sourceOf);
}

function sourceOf(source: unknown): FindingSource[] {
  if (typeof source !== "object" || source === null) return [];
  const { url, quotes } = source as { [key: string]: unknown };
  if (typeof url !== "string") return [];
  return [{ url, quotes: quotesOf(quotes) }];
}

function quotesOf(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((quote): quote is string => typeof quote === "string");
}
