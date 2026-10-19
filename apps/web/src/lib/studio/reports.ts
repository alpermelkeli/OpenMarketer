/**
 * What the reviews of a studio version found, as a screen shows them: the programmatic checks
 * (its size, format, bytes and words, measured by code) and the visual verifier's criteria.
 *
 * Both reports arrive from the API as plain data that a run wrote, so each is read
 * defensively: an entry without the shape the studio writes is left out rather than shown
 * half-way, and every text in them is untrusted, to be rendered as text only. Display only;
 * whether a version passed is the run's decision, and `passed` here is what it recorded.
 */

import type { MediaAssetVersion } from "@/lib/api/types";

import { formatBytes } from "./status";

/** One thing the checks measured, with whether it held and what was found. */
export type CheckItem = {
  label: string;
  passed: boolean;
  /** True when code could not settle it and nothing else did: neither passed nor failed. */
  unconfirmed?: boolean;
  /** What was measured, or what went wrong, in words. */
  detail: string;
  /** Lines of the request's copy OCR read exactly: text a person typed. */
  confirmed?: readonly string[];
  /** Lines of the request's copy not confirmed as asked: text a person typed. */
  lines?: readonly CopyLine[];
};

/**
 * A line of copy OCR did not confirm: what it should read, what OCR read of it (`null` when it
 * read nothing close), and what the verifier read of it with whether that is the line (`null`
 * when the verifier gave no verdict on it).
 */
export type CopyLine = {
  expected: string;
  read: string | null;
  verifier: { read: string | null; matches: boolean } | null;
  /** What the verifier made of the line, in words; `null` on a report from before it was asked. */
  verdict: string | null;
};

export type CheckReport = {
  passed: boolean | null;
  items: CheckItem[];
  /** Failures the dashboard has no words for, by the name the run gave them. */
  other: string[];
  /** Notices the dashboard has no words for, by the name the run gave them. */
  otherNotices: string[];
};

const FAILURE_FIELDS = new Set(["size", "format", "bytes", "unreadable", "copy", "copy_unread"]);
const NOTICE_FIELDS = new Set(["copy_unconfirmed"]);

/**
 * The checks by code of a version, with the verifier's verdict on the lines of copy OCR could
 * not confirm when the verifier was asked (it is in `verifierReport`, not in the check's report).
 */
export function checkReportOf(
  report: MediaAssetVersion["check_report"],
  verifierReport: MediaAssetVersion["verifier_report"] = null,
): CheckReport | null {
  if (report === null || typeof report !== "object") return null;
  const failures = stringsOf(report.failures);
  const failed = (name: string) => failures.includes(name);
  const items: CheckItem[] = [];

  const width = numberOf(report.width);
  const height = numberOf(report.height);
  if (width !== null && height !== null) {
    items.push({
      label: "Size",
      passed: !failed("size"),
      detail: `${width} × ${height} pixels${failed("size") ? ", not what the platform takes" : ""}`,
    });
  }
  const mime = typeof report.mime === "string" ? report.mime : null;
  if (mime !== null) {
    items.push({
      label: "Format",
      passed: !failed("format"),
      detail: failed("format") ? `${mime}, which the platform does not take` : mime,
    });
  }
  const bytes = numberOf(report.size_bytes);
  if (bytes !== null) {
    items.push({
      label: "File size",
      passed: !failed("bytes"),
      detail: failed("bytes") ? `${formatBytes(bytes)}, more than the platform takes` : formatBytes(bytes),
    });
  }
  if (failed("unreadable")) {
    items.push({ label: "Readable file", passed: false, detail: "The file could not be read as an image." });
  }
  const copy = copyItemOf(report.copy, failed("copy_unread"), copyVerdictOf(verifierReport));
  if (copy !== null) items.push(copy);

  return {
    passed: typeof report.passed === "boolean" ? report.passed : null,
    items,
    other: failures.filter((name) => !FAILURE_FIELDS.has(name) && !name.startsWith("verifier:")),
    otherNotices: stringsOf(report.notices).filter((name) => !NOTICE_FIELDS.has(name)),
  };
}

type VerdictLine = { expected: string; transcribed: string | null; matches: boolean };

/** The verifier's verdict on each line OCR did not confirm; `null` when it gave none. */
function copyVerdictOf(report: MediaAssetVersion["verifier_report"]): VerdictLine[] | null {
  if (report === null || typeof report !== "object" || !Array.isArray(report.copy_verdict)) return null;
  return report.copy_verdict.flatMap((entry): VerdictLine[] =>
    isRecord(entry) && typeof entry.expected === "string" && typeof entry.matches === "boolean"
      ? [{ expected: entry.expected, transcribed: textOf(entry.transcribed), matches: entry.matches }]
      : [],
  );
}

function copyItemOf(copy: unknown, unread: boolean, verdict: VerdictLine[] | null): CheckItem | null {
  if (unread || (isRecord(copy) && copy.unread === true)) {
    return { label: "Words", passed: false, detail: "The words in the image could not be read." };
  }
  if (!isRecord(copy)) return null;
  if (Array.isArray(copy.confirmed) || Array.isArray(copy.unconfirmed)) {
    return confirmedCopyItemOf(copy, verdict);
  }
  return comparedCopyItemOf(copy);
}

/** A report that says which lines OCR confirmed, the others left to the verifier. */
function confirmedCopyItemOf(copy: Record<string, unknown>, verdict: VerdictLine[] | null): CheckItem {
  const confirmed = stringsOf(copy.confirmed);
  const lines = (Array.isArray(copy.unconfirmed) ? copy.unconfirmed : []).flatMap((entry): CopyLine[] => {
    if (!isRecord(entry) || typeof entry.expected !== "string") return [];
    const judged = verdict?.find((line) => line.expected === entry.expected);
    const verifier = judged === undefined ? null : { read: judged.transcribed, matches: judged.matches };
    return [{ expected: entry.expected, read: textOf(entry.read), verifier, verdict: verdictWords(verifier) }];
  });
  const total = confirmed.length + lines.length;
  const ocr = `OCR confirmed ${confirmed.length} of ${total} line${total === 1 ? "" : "s"} of the copy`;
  if (lines.length === 0) {
    return { label: "Words", passed: true, detail: "OCR read every line of the copy as asked.", confirmed };
  }
  const wrong = lines.filter((line) => line.verifier?.matches === false).length;
  if (wrong > 0) {
    return {
      label: "Words",
      passed: false,
      detail: `${ocr}; the verifier read ${wrong} of the others differently from what was asked.`,
      confirmed,
      lines,
    };
  }
  const unjudged = lines.filter((line) => line.verifier === null).length;
  if (unjudged > 0) {
    return {
      label: "Words",
      passed: false,
      unconfirmed: true,
      detail: `${ocr}; no one has confirmed ${unjudged === lines.length ? "the others" : `${unjudged} of the others`}.`,
      confirmed,
      lines,
    };
  }
  return {
    label: "Words",
    passed: true,
    detail: `${ocr}; the verifier read the others as asked.`,
    confirmed,
    lines,
  };
}

function verdictWords(verifier: CopyLine["verifier"]): string {
  if (verifier === null) return "The verifier gave no verdict on this line, so it is not confirmed.";
  const outcome = verifier.matches ? "matches" : "does not match";
  if (verifier.read === null) return `The verifier gave no reading of it — ${outcome}`;
  return `The verifier read “${verifier.read}” — ${outcome}`;
}

/** A report written before OCR's reading was evidence: lines missing or misspelled. */
function comparedCopyItemOf(copy: Record<string, unknown>): CheckItem {
  const missing = stringsOf(copy.missing).map((expected): CopyLine => ({ expected, read: null, verifier: null, verdict: null }));
  const misspelled = Array.isArray(copy.misspelled)
    ? copy.misspelled.flatMap((entry): CopyLine[] =>
        isRecord(entry) && typeof entry.expected === "string"
          ? [{ expected: entry.expected, read: textOf(entry.read), verifier: null, verdict: null }]
          : [],
      )
    : [];
  const lines = [...missing, ...misspelled];
  if (lines.length === 0) {
    return { label: "Words", passed: copy.passed !== false, detail: "Every line of the copy reads as asked." };
  }
  return {
    label: "Words",
    passed: false,
    detail: `${lines.length} line${lines.length === 1 ? "" : "s"} of the copy ${lines.length === 1 ? "does" : "do"} not read as asked.`,
    lines,
  };
}

/** One criterion the verifier judged. */
export type Criterion = {
  label: string;
  passed: boolean;
  /** "none", "minor", "major" or "critical", in words; a minor failure still lets a version pass. */
  severity: string;
  /** The verifier's note: model output, text only. */
  note: string;
};

export type VerifierReport = { criteria: Criterion[]; suggestions: string[] };

const CRITERIA = new Map<string, string>([
  ["legibility", "Legibility"],
  ["copy_rendered", "Copy shown as written"],
  ["brand_fit", "Fits the brand"],
  ["artifacts", "Free of glitches"],
  ["composition", "Composition"],
  ["risky_content", "Nothing risky or misleading"],
]);

const SEVERITIES = new Map<string, string>([
  ["none", "No issue"],
  ["minor", "Minor"],
  ["major", "Major"],
  ["critical", "Critical"],
]);

export function verifierReportOf(report: MediaAssetVersion["verifier_report"]): VerifierReport | null {
  if (report === null || typeof report !== "object") return null;
  const criteria = Array.isArray(report.criteria)
    ? report.criteria.flatMap((entry): Criterion[] => {
        if (!isRecord(entry) || typeof entry.criterion !== "string" || typeof entry.passed !== "boolean") return [];
        const severity = typeof entry.severity === "string" ? entry.severity : "";
        return [
          {
            label: CRITERIA.get(entry.criterion) ?? entry.criterion,
            passed: entry.passed,
            severity: SEVERITIES.get(severity) ?? severity,
            note: typeof entry.note === "string" ? entry.note : "",
          },
        ];
      })
    : [];
  return { criteria, suggestions: stringsOf(report.edit_suggestions) };
}

/** A round of a run: the versions it made, oldest first. */
export type Round = { round: number; versions: MediaAssetVersion[] };

/** The versions grouped by the round that made them, the first round first. */
export function roundsOf(versions: readonly MediaAssetVersion[]): Round[] {
  const byRound = new Map<number, MediaAssetVersion[]>();
  for (const version of versions) {
    const round = byRound.get(version.round) ?? [];
    round.push(version);
    byRound.set(version.round, round);
  }
  return [...byRound.entries()]
    .sort(([a], [b]) => a - b)
    .map(([round, made]) => ({
      round,
      versions: [...made].sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at)),
    }));
}

/** What every version of the asset cost together. */
export function versionsCost(versions: readonly MediaAssetVersion[]): number {
  return versions.reduce((total, version) => total + version.cost_usd, 0);
}

function stringsOf(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

/** A text, or `null` when there is none: an empty reading is no reading. */
function textOf(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function numberOf(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
