/** Query keys of the server state the dashboard holds, in one place so invalidation cannot drift. */

export const queryKeys = {
  projects: () => ["projects", "list"] as const,
  /** Everything held about one project: the project itself, its runs and its profile versions. */
  projectScope: (projectId: string) => ["projects", "one", projectId] as const,
  project: (projectId: string) => ["projects", "one", projectId, "project"] as const,
  analyses: (projectId: string) => ["projects", "one", projectId, "analyses", "list"] as const,
  analysisRun: (projectId: string, runId: string) =>
    ["projects", "one", projectId, "analyses", "run", runId] as const,
  profileVersions: (projectId: string) => ["projects", "one", projectId, "versions", "list"] as const,
  profileVersion: (projectId: string, version: number) =>
    ["projects", "one", projectId, "versions", "one", version] as const,
};
