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
  /** What was measured, or what went wrong, in words. */
  detail: string;
  /** Lines of the request's copy the image does not show as asked: text a person typed. */
  lines?: readonly CopyLine[];
};

export type CopyLine = { expected: string; read: string | null };

export type CheckReport = {
  passed: boolean | null;
  items: CheckItem[];
  /** Failures the dashboard has no words for, by the name the run gave them. */
  other: string[];
};

const FAILURE_FIELDS = new Set(["size", "format", "bytes", "unreadable", "copy", "copy_unread"]);

export function checkReportOf(report: MediaAssetVersion["check_report"]): CheckReport | null {
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
  const copy = copyItemOf(report.copy, failed("copy_unread"));
  if (copy !== null) items.push(copy);

  return {
    passed: typeof report.passed === "boolean" ? report.passed : null,
    items,
    other: failures.filter((name) => !FAILURE_FIELDS.has(name) && !name.startsWith("verifier:")),
  };
}

function copyItemOf(copy: unknown, unread: boolean): CheckItem | null {
  if (unread || (isRecord(copy) && copy.unread === true)) {
    return { label: "Words", passed: false, detail: "The words in the image could not be read." };
  }
  if (!isRecord(copy)) return null;
  const missing = stringsOf(copy.missing).map((expected) => ({ expected, read: null }));
  const misspelled = Array.isArray(copy.misspelled)
    ? copy.misspelled.flatMap((entry): CopyLine[] =>
        isRecord(entry) && typeof entry.expected === "string"
          ? [{ expected: entry.expected, read: typeof entry.read === "string" ? entry.read : null }]
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

function numberOf(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
