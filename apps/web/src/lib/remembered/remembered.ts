/**
 * What this browser remembers about projects and runs: a stop-gap.
 *
 * The API cannot list projects, read one by id, or list a project's runs yet, so
 * the dashboard keeps the projects and runs it has seen itself, in this browser's
 * local storage. It is a notebook, not a source of truth: another browser starts
 * empty, and clearing site data forgets everything while the projects stay on the
 * server. Delete this module when the API has list routes.
 *
 * This file is the pure part: the shape, reading it defensively and changing it.
 */

import type { Project } from "@/lib/api/types";

export type Remembered = {
  /** Projects created here, as the API returned them. Newest first. */
  projects: readonly Project[];
  /** Projects opened by id: the API cannot tell their name or repository. */
  openedProjectIds: readonly string[];
  /** Run ids by project, newest first. Their state is always read from the API. */
  runIdsByProject: Readonly<Record<string, readonly string[]>>;
};

export const NOTHING_REMEMBERED: Remembered = {
  projects: [],
  openedProjectIds: [],
  runIdsByProject: {},
};

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isProjectId(value: string): boolean {
  return UUID.test(value);
}

/** Read what was stored. Storage can be edited by hand, so nothing in it is assumed. */
export function parseRemembered(stored: string | null): Remembered {
  if (stored === null) return NOTHING_REMEMBERED;
  let value: unknown;
  try {
    value = JSON.parse(stored);
  } catch {
    return NOTHING_REMEMBERED;
  }
  if (!isRecord(value)) return NOTHING_REMEMBERED;

  const runIdsByProject: Record<string, string[]> = {};
  if (isRecord(value.runIdsByProject)) {
    for (const [projectId, runIds] of Object.entries(value.runIdsByProject)) {
      if (isProjectId(projectId)) runIdsByProject[projectId] = idsIn(runIds);
    }
  }
  return {
    projects: Array.isArray(value.projects) ? value.projects.filter(isProject) : [],
    openedProjectIds: idsIn(value.openedProjectIds),
    runIdsByProject,
  };
}

export function findProject(remembered: Remembered, projectId: string): Project | null {
  return remembered.projects.find((project) => project.id === projectId) ?? null;
}

export function isRemembered(remembered: Remembered, projectId: string): boolean {
  return (
    findProject(remembered, projectId) !== null || remembered.openedProjectIds.includes(projectId)
  );
}

export function withProject(remembered: Remembered, project: Project): Remembered {
  return {
    ...remembered,
    projects: [project, ...remembered.projects.filter((p) => p.id !== project.id)],
    openedProjectIds: remembered.openedProjectIds.filter((id) => id !== project.id),
  };
}

export function withOpenedProject(remembered: Remembered, projectId: string): Remembered {
  if (isRemembered(remembered, projectId)) return remembered;
  return { ...remembered, openedProjectIds: [projectId, ...remembered.openedProjectIds] };
}

export function withRun(remembered: Remembered, projectId: string, runId: string): Remembered {
  const known = remembered.runIdsByProject[projectId] ?? [];
  if (known.includes(runId)) return remembered;
  return {
    ...remembered,
    runIdsByProject: { ...remembered.runIdsByProject, [projectId]: [runId, ...known] },
  };
}

/** Forget a project here. The project and its profile stay on the server. */
export function withoutProject(remembered: Remembered, projectId: string): Remembered {
  const runIdsByProject = { ...remembered.runIdsByProject };
  delete runIdsByProject[projectId];
  return {
    projects: remembered.projects.filter((project) => project.id !== projectId),
    openedProjectIds: remembered.openedProjectIds.filter((id) => id !== projectId),
    runIdsByProject,
  };
}

function isProject(value: unknown): value is Project {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    isProjectId(value.id) &&
    typeof value.name === "string" &&
    typeof value.repository_url === "string" &&
    typeof value.created_at === "string"
  );
}

function idsIn(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((id): id is string => typeof id === "string" && isProjectId(id));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
