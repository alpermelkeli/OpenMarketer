/**
 * What a studio run was asked for, as its page shows it. A run's own route serves its request;
 * a list does not, so a run first shown from a list has none until its own route has answered,
 * and a request stored in a form the API can no longer read is served as null. The goal, the
 * lines of copy and the notes are what a person or a model wrote: the page shows them as text.
 *
 * Display only.
 */

import type { StudioRun, StudioRunDetail, StudioRunRequest } from "@/lib/api/types";

export type RunBrief =
  /** The run as a list gave it: its own route has not answered yet. */
  | { state: "loading" }
  /** The API holds a request it cannot serve. */
  | { state: "unreadable" }
  | { state: "known"; request: StudioRunRequest };

export function runBrief(run: StudioRun | StudioRunDetail): RunBrief {
  if (!("request" in run)) return { state: "loading" };
  return run.request === null ? { state: "unreadable" } : { state: "known", request: run.request };
}

/** The goal, for the image's alt text, once the request is known. */
export function briefGoal(brief: RunBrief): string | undefined {
  return brief.state === "known" ? brief.request.goal : undefined;
}

/** The lines of copy that are not blank, as they are to be read on the image. */
export function copyLines(request: StudioRunRequest): string[] {
  return request.exact_copy.filter((line) => line.trim() !== "");
}

/** "No notes on style" for blank notes, the notes otherwise. */
export function styleNotesText(request: StudioRunRequest): string {
  const notes = request.style_notes.trim();
  return notes === "" ? "No notes on style." : notes;
}

/** How many of the library's assets the run was asked to look at, in words. */
export function referencesText(request: StudioRunRequest): string {
  const n = request.reference_asset_ids.length;
  if (n === 0) return "No image of the library.";
  return n === 1 ? "One image of the library." : `${n} images of the library.`;
}
