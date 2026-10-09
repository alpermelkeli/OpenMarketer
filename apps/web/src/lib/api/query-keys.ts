/** Query keys of the server state the dashboard holds, in one place so invalidation cannot drift. */

export const queryKeys = {
  analysisRun: (projectId: string, runId: string) => ["projects", projectId, "analyses", runId] as const,
  draftProfile: (projectId: string) => ["projects", projectId, "profile", "draft"] as const,
  approvedProfile: (projectId: string) => ["projects", projectId, "profile", "approved"] as const,
  profile: (projectId: string) => ["projects", projectId, "profile"] as const,
};
