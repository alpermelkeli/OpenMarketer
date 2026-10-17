/**
 * Where an address is inside a project: which project it names and which of the project's
 * pages it shows. The sidebar and the project's frame read the address through this; it
 * makes no request and says nothing about whether the project exists.
 */

import { isProjectId } from "./project-id";

export type ProjectPage = "chat" | "profile" | "analyses" | "research" | "approvals";

/** The project's pages, in the order the sidebar lists them under the project. */
export const PROJECT_PAGES: readonly {
  page: Exclude<ProjectPage, "chat">;
  label: string;
}[] = [
  { page: "profile", label: "Profile" },
  { page: "analyses", label: "Analyses" },
  { page: "research", label: "Research" },
  { page: "approvals", label: "Approvals" },
];

/** The project an address is inside, or null outside a project or for a segment that is no project's ID. */
export function projectIdOfPath(pathname: string): string | null {
  const segment = /^\/projects\/([^/]+)/.exec(pathname)?.[1];
  return segment !== undefined && isProjectId(segment) ? segment : null;
}

/** The page of the project an address shows, or null for the project's own address and anything else. */
export function projectPageOfPath(pathname: string): ProjectPage | null {
  const page = /^\/projects\/[^/]+\/([^/]+)/.exec(pathname)?.[1];
  return page === "chat" || PROJECT_PAGES.some((p) => p.page === page) ? (page as ProjectPage) : null;
}

/** The chat an address shows, or null for a new chat and anything else. */
export function chatIdOfPath(pathname: string): string | null {
  return /^\/projects\/[^/]+\/chat\/([^/]+)$/.exec(pathname)?.[1] ?? null;
}

/** The address of one of the project's chats. */
export function chatHref(projectId: string, chatId: string): string {
  return `${projectPageHref(projectId, "chat")}/${chatId}`;
}

/** The address of one of the project's research runs. */
export function researchHref(projectId: string, runId: string): string {
  return `${projectPageHref(projectId, "research")}/${runId}`;
}

/** The address of one of a project's pages. */
export function projectPageHref(projectId: string, page: ProjectPage): string {
  return `/projects/${projectId}/${page}`;
}

/** A repository's address as the sidebar shows it under the project's name: no scheme, no well-known forge's host. */
export function shortRepositoryName(repositoryUrl: string): string {
  return repositoryUrl.replace(/^https?:\/\/(www\.)?(github\.com|gitlab\.com)\//, "").replace(/\.git$/, "");
}
