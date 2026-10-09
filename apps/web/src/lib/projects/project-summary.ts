/**
 * Where a project stands, said in a few words on the projects list. The facts are
 * the API's summary fields; this only words them.
 */

import type { Tone } from "@/lib/analysis/run-status";
import type { Project } from "@/lib/api/types";

export type ProjectState = { label: string; tone: Tone };

type Summary = Pick<Project, "latest_draft_version" | "latest_approved_version" | "unfinished_run_id">;

/** The version a reviewer should look at first: a draft newer than what is approved, if there is one. */
export function draftAwaitingReview(project: Summary): number | null {
  const draft = project.latest_draft_version;
  if (draft === null) return null;
  return draft > (project.latest_approved_version ?? 0) ? draft : null;
}

/** Everything worth saying about the project, the most pressing first. */
export function projectStates(project: Summary): ProjectState[] {
  const states: ProjectState[] = [];
  if (project.unfinished_run_id !== null) {
    states.push({ label: "Analysis in progress", tone: "progress" });
  }
  const awaiting = draftAwaitingReview(project);
  if (awaiting !== null) {
    states.push({ label: `Draft version ${awaiting} awaits review`, tone: "warning" });
  }
  if (project.latest_approved_version !== null) {
    states.push({ label: `Approved version ${project.latest_approved_version}`, tone: "success" });
  }
  if (states.length === 0) {
    states.push({ label: "Not analysed yet", tone: "neutral" });
  }
  return states;
}
