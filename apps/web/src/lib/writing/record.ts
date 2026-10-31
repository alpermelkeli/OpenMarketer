/**
 * The record of how a post was written, as a screen shows it: the plan, the claims with the
 * evidence of their features, the code-rule report, the critic's report and the other variants.
 *
 * The API serves these as plain data that a run wrote (`dict`s in its contract), so each is read
 * defensively here and nowhere else: an entry without the shape the writer stores is left out
 * rather than shown half-way, and a rule, criterion or format the dashboard has no words for is
 * shown by the name the run gave it. Every text in them was written by a model or taken from a
 * repository: untrusted, to be rendered as text only. Display only: whether a variant kept a rule
 * or passed a criterion is what the run recorded, never worked out here.
 */

import type { ContentItemDetail } from "@/lib/api/types";

/** One rule of the code the post broke, in words, with what the check found. */
export type RuleFinding = { rule: string; label: string; finding: string };

export type CheckReport = {
  /** Whether the text kept every rule of the code, as the run recorded it; null when it did not say. */
  passed: boolean | null;
  /** The text's length as X counts it (weighted), as the check measured it. */
  length: number | null;
  failures: RuleFinding[];
  /** Numbers in the text that the profile does not hold, for a person to check. */
  flaggedNumbers: string[];
};

export type CriterionVerdict = {
  label: string;
  passed: boolean;
  /** "No issue", "Minor" or "Major", in words; a minor failure is advice. */
  severity: string;
  /** Whether the failure is only advice: the text could be posted as it is. */
  minor: boolean;
  /** The critic's note: model output, text only. */
  note: string;
};

export type Review = {
  criteria: CriterionVerdict[];
  /** Whether the critic found a risky claim; null when it did not say. */
  riskyClaim: boolean | null;
  riskyNote: string;
  suggestions: string[];
};

/** Where in the repository a feature a claim rests on was found. */
export type ClaimEvidence = { featureId: string; file: string; lines: string | null };

export type Claim = { statement: string; featureIds: string[]; evidence: ClaimEvidence[] };

export type Plan = {
  angle: string | null;
  /** The format in words, or the run's own name for one the dashboard does not know. */
  format: string | null;
  featureIds: string[];
};

/** Another variant of the round the post was chosen from, with what the checks found of it. */
export type Alternative = {
  /** Its number in the round, from 0, as the run and the critic name it. */
  index: number;
  text: string;
  angle: string;
  language: string;
  claims: { statement: string; featureIds: string[] }[];
  check: CheckReport | null;
  /** Null when the critic did not review it. */
  review: Review | null;
};

const RULES = new Map<string, string>([
  ["length", "Length on X"],
  ["do_not_mention", "Nothing the profile says never to mention"],
  ["claims", "Every claim rests on a live feature"],
  ["no_links", "No links"],
  ["no_mentions", "No @mentions"],
  ["hashtags", "Hashtags within X's limit"],
  ["language", "One of the product's languages"],
]);

const CRITERIA = new Map<string, string>([
  ["brand_voice", "The brand's voice"],
  ["facts", "Facts"],
  ["platform_fit", "Reads well on X"],
  ["brief_fit", "Does what the brief asks"],
  ["language", "Written in its language"],
]);

const SEVERITIES = new Map<string, string>([
  ["none", "No issue"],
  ["minor", "Minor"],
  ["major", "Major"],
]);

const FORMATS = new Map<string, string>([
  ["statement", "A statement and why it matters"],
  ["question", "Opens with a question"],
  ["tip", "A practical tip"],
  ["list", "A short list"],
  ["story", "A small before-and-after"],
]);

export function checkReportOf(report: unknown): CheckReport | null {
  if (!isRecord(report)) return null;
  const failures = Array.isArray(report.failures)
    ? report.failures.flatMap((entry): RuleFinding[] =>
        isRecord(entry) && typeof entry.rule === "string" && typeof entry.finding === "string"
          ? [{ rule: entry.rule, label: RULES.get(entry.rule) ?? entry.rule, finding: entry.finding }]
          : [],
      )
    : [];
  return {
    passed: typeof report.passed === "boolean" ? report.passed : null,
    length: countOf(report.length),
    failures,
    flaggedNumbers: stringsOf(report.flagged_numbers),
  };
}

/** The rule of the code that holds a text to X's length. */
const LENGTH_RULE = "length";

export type PostLength = {
  /** The length as X counts it, as the check measured it. */
  count: number;
  /** Whether the text kept X's limit, as the check recorded it; null when it did not say. */
  kept: boolean | null;
};

/**
 * The text's length on X and whether it kept the limit, read from its code-rule report: the
 * count is weighted (some characters count twice), so it is the check's, never worked out here.
 * Null when the report gives no length.
 */
export function postLengthOf(report: CheckReport | null): PostLength | null {
  if (report === null || report.length === null) return null;
  const broken = report.failures.some((failure) => failure.rule === LENGTH_RULE);
  return { count: report.length, kept: broken ? false : report.passed === null ? null : true };
}

export function reviewOf(review: unknown): Review | null {
  if (!isRecord(review)) return null;
  const criteria = Array.isArray(review.criteria)
    ? review.criteria.flatMap((entry): CriterionVerdict[] => {
        if (!isRecord(entry) || typeof entry.criterion !== "string" || typeof entry.passed !== "boolean") return [];
        const severity = typeof entry.severity === "string" ? entry.severity : "";
        return [
          {
            label: CRITERIA.get(entry.criterion) ?? entry.criterion,
            passed: entry.passed,
            severity: SEVERITIES.get(severity) ?? severity,
            minor: severity === "minor",
            note: typeof entry.note === "string" ? entry.note : "",
          },
        ];
      })
    : [];
  return {
    criteria,
    riskyClaim: typeof review.risky_claim === "boolean" ? review.risky_claim : null,
    riskyNote: typeof review.risky_note === "string" ? review.risky_note : "",
    suggestions: stringsOf(review.suggestions),
  };
}

export function claimsOf(claims: ContentItemDetail["claims"]): Claim[] {
  return claims.flatMap((entry): Claim[] => {
    if (!isRecord(entry) || typeof entry.statement !== "string") return [];
    const evidence = Array.isArray(entry.evidence)
      ? entry.evidence.flatMap((item): ClaimEvidence[] =>
          isRecord(item) && typeof item.file === "string" && item.file !== ""
            ? [
                {
                  featureId: typeof item.feature_id === "string" ? item.feature_id : "",
                  file: item.file,
                  lines: typeof item.lines === "string" && item.lines !== "" ? item.lines : null,
                },
              ]
            : [],
        )
      : [];
    return [{ statement: entry.statement, featureIds: stringsOf(entry.feature_ids), evidence }];
  });
}

export function planOf(plan: ContentItemDetail["plan"]): Plan {
  const format = typeof plan.format === "string" ? (FORMATS.get(plan.format) ?? plan.format) : null;
  return {
    angle: typeof plan.angle === "string" && plan.angle !== "" ? plan.angle : null,
    format,
    featureIds: stringsOf(plan.feature_ids),
  };
}

/** The other variants, in the order of the round. */
export function alternativesOf(alternatives: ContentItemDetail["alternatives"]): Alternative[] {
  return alternatives
    .flatMap((entry): Alternative[] => {
      if (!isRecord(entry) || !isRecord(entry.variant)) return [];
      const { variant } = entry;
      const index = countOf(entry.index);
      if (index === null || typeof variant.text !== "string") return [];
      return [
        {
          index,
          text: variant.text,
          angle: typeof variant.angle === "string" ? variant.angle : "",
          language: typeof variant.language === "string" ? variant.language : "",
          claims: Array.isArray(variant.claims)
            ? variant.claims.flatMap((claim) =>
                isRecord(claim) && typeof claim.statement === "string"
                  ? [{ statement: claim.statement, featureIds: stringsOf(claim.feature_ids) }]
                  : [],
              )
            : [],
          check: checkReportOf(entry.check),
          review: reviewOf(entry.review),
        },
      ];
    })
    .sort((a, b) => a.index - b.index);
}

/** "Variant 2" for the round's variant 1: people count from one. */
export function variantLabel(index: number): string {
  return `Variant ${index + 1}`;
}

function stringsOf(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function countOf(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
