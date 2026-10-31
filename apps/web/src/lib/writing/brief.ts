/**
 * What a writing run was asked for, as its page shows it. A run's own route serves its brief; a
 * list does not, so a run first shown from a list has none until its own route has answered, and
 * a brief stored in a form the API can no longer read is served as null. The angle is what a
 * person wrote: the page shows it as text.
 *
 * Display only.
 */

import type { WritingBrief, WritingRun, WritingRunDetail } from "@/lib/api/types";

export type RunBrief =
  /** The run as a list gave it: its own route has not answered yet. */
  | { state: "loading" }
  /** The API holds a brief it cannot serve. */
  | { state: "unreadable" }
  | { state: "known"; brief: WritingBrief };

export function runBrief(run: WritingRun | WritingRunDetail): RunBrief {
  if (!("brief" in run)) return { state: "loading" };
  return run.brief === null ? { state: "unreadable" } : { state: "known", brief: run.brief };
}

/** "3 texts to choose from", "One text". */
export function variantsText(variants: number): string {
  return variants === 1 ? "One text" : `${variants} texts to choose from`;
}
