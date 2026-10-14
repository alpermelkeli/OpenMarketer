/** Query keys of the server state the dashboard holds, in one place so invalidation cannot drift. */

export const queryKeys = {
  projects: () => ["projects", "list"] as const,
  /** Everything held about one project: the project itself, its runs, its profile versions and its proposals. */
  projectScope: (projectId: string) => ["projects", "one", projectId] as const,
  project: (projectId: string) => ["projects", "one", projectId, "project"] as const,
  analyses: (projectId: string) => ["projects", "one", projectId, "analyses", "list"] as const,
  analysisRun: (projectId: string, runId: string) =>
    ["projects", "one", projectId, "analyses", "run", runId] as const,
  profileVersions: (projectId: string) => ["projects", "one", projectId, "versions", "list"] as const,
  profileVersion: (projectId: string, version: number) =>
    ["projects", "one", projectId, "versions", "one", version] as const,
  proposals: (projectId: string) => ["projects", "one", projectId, "proposals", "list"] as const,
  /** Where each credential's value comes from. Never a value: no response holds one. */
  credentials: () => ["settings", "credentials"] as const,
  /** Everything about the models: the settings, and the check that depends on them. */
  modelScope: () => ["settings", "models"] as const,
  modelSettings: () => ["settings", "models", "settings"] as const,
  modelCheck: () => ["settings", "models", "check"] as const,
  /** The provider's catalogue: it does not change with the workspace's choices. */
  modelCatalog: () => ["settings", "model-catalog"] as const,
  analysisSettings: () => ["settings", "analysis"] as const,
};
