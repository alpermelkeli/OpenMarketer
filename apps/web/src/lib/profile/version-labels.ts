/** Words and short forms for a profile version's facts. Display only. */

import type { Project } from "@/lib/api/types";

import { draftAwaitingReview } from "@/lib/projects/project-summary";

/** The first seven characters of a commit id, as hosts and git show it. */
export function shortCommit(commitSha: string): string {
  return commitSha.slice(0, 7);
}

/** A version number from the address, or nothing if it is not one. */
export function versionFromAddress(value: string | null): number | null {
  return value !== null && /^[1-9][0-9]{0,8}$/.test(value) ? Number(value) : null;
}

/**
 * The version to open when the address names none: a draft that awaits review, else the
 * latest approved version, else whatever draft there is. Nothing for a project without a profile.
 */
export function versionToOpen(
  project: Pick<Project, "latest_draft_version" | "latest_approved_version" | "unfinished_run_id">,
): number | null {
  return draftAwaitingReview(project) ?? project.latest_approved_version ?? project.latest_draft_version;
}
