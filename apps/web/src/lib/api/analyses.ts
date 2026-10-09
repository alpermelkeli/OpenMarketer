"use client";

/** Query and mutation hooks for analysis runs. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { isRunFinished } from "@/lib/analysis/run-status";

import type { ApiError } from "./api-error";
import { api, dataOf } from "./client";
import { queryKeys } from "./query-keys";
import type { AnalysisRun } from "./types";

const POLL_INTERVAL_MS = 2_000;

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
  });
}

/** One run, polled until it has finished. A finished run never changes, so it is not refetched. */
export function useAnalysisRun(projectId: string, runId: string) {
  const queryClient = useQueryClient();
  return useQuery<AnalysisRun, ApiError>({
    queryKey: queryKeys.analysisRun(projectId, runId),
    queryFn: async () => {
      const run = dataOf(
        await api.GET("/v1/projects/{project_id}/analyses/{run_id}", {
          params: { path: { project_id: projectId, run_id: runId } },
        }),
      );
      if (run.status === "succeeded") {
        // The run stored a new draft; whatever the profile screen holds is stale.
        void queryClient.invalidateQueries({ queryKey: queryKeys.profile(projectId) });
      }
      return run;
    },
    refetchInterval: (query) => {
      const run = query.state.data;
      return run !== undefined && isRunFinished(run) ? false : POLL_INTERVAL_MS;
    },
    staleTime: (query) => {
      const run = query.state.data;
      return run !== undefined && isRunFinished(run) ? Infinity : 0;
    },
    retry: false,
  });
}
