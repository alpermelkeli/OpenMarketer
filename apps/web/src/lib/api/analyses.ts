"use client";

/** Query and mutation hooks for analysis runs. */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { isRunFinished } from "@/lib/analysis/run-status";

import type { ApiError } from "./api-error";
import { api, dataOf } from "./client";
import { PAGE_SIZE, mergePages, nextCursor } from "./pages";
import { queryKeys } from "./query-keys";
import type { AnalysisRun, AnalysisRunList } from "./types";

const POLL_INTERVAL_MS = 2_000;

/** A project's runs, newest first, loaded a page at a time. */
export function useAnalyses(projectId: string) {
  return useInfiniteQuery<
    AnalysisRunList,
    ApiError,
    AnalysisRun[],
    ReturnType<typeof queryKeys.analyses>,
    string | undefined
  >({
    queryKey: queryKeys.analyses(projectId),
    queryFn: async ({ pageParam }) =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/analyses", {
          params: { path: { project_id: projectId }, query: { limit: PAGE_SIZE, cursor: pageParam } },
        }),
      ),
    initialPageParam: undefined,
    getNextPageParam: nextCursor,
    select: (data) =>
      mergePages(
        data.pages.map((page) => page.runs),
        (run) => run.id,
      ),
    retry: false,
  });
}

export function useStartAnalysis(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<AnalysisRun, ApiError, void>({
    mutationFn: async () =>
      dataOf(
        await api.POST("/v1/projects/{project_id}/analyses", {
          params: { path: { project_id: projectId } },
        }),
      ),
    onSuccess: (run) => {
      queryClient.setQueryData(queryKeys.analysisRun(projectId, run.id), run);
    },
    // After a refusal too: a 409 means a run this screen may not know of is in progress,
    // and a 503 leaves a failed run behind.
    onSettled: () => refreshProject(queryClient, projectId),
  });
}

/**
 * One run, polled until it has finished. `listed` is the run as the list gave it, so
 * it is on screen at once; a finished run never changes and is not asked for again.
 */
export function useAnalysisRun(projectId: string, listed: AnalysisRun) {
  const queryClient = useQueryClient();
  return useQuery<AnalysisRun, ApiError>({
    queryKey: queryKeys.analysisRun(projectId, listed.id),
    queryFn: async () => {
      const run = dataOf(
        await api.GET("/v1/projects/{project_id}/analyses/{run_id}", {
          params: { path: { project_id: projectId, run_id: listed.id } },
        }),
      );
      if (isRunFinished(run)) {
        // The list, the project's summary and, after a success, its versions are now stale.
        void refreshProject(queryClient, projectId);
      }
      return run;
    },
    initialData: listed,
    enabled: !isRunFinished(listed),
    refetchInterval: (query) => {
      const run = query.state.data;
      return run !== undefined && isRunFinished(run) ? false : POLL_INTERVAL_MS;
    },
    staleTime: 0,
    retry: false,
  });
}

/** Read again what the dashboard holds about a project, except the runs being polled. */
function refreshProject(queryClient: QueryClient, projectId: string): Promise<void> {
  return queryClient.invalidateQueries({
    queryKey: queryKeys.projectScope(projectId),
    predicate: (query) => query.queryKey[4] !== "run",
  });
}
