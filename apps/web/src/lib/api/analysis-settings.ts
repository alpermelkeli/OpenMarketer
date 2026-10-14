"use client";

/** Query and mutation hooks for the limits of the workspace's analyses. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { ApiError } from "./api-error";
import { api, dataOf, doneOf } from "./client";
import { queryKeys } from "./query-keys";
import type { AnalysisLimitsRequest, AnalysisSettings } from "./types";

/** The limits a run gets, the defaults, and what the workspace chose. */
export function useAnalysisSettings() {
  return useQuery<AnalysisSettings, ApiError>({
    queryKey: queryKeys.analysisSettings(),
    queryFn: async () => dataOf(await api.GET("/v1/settings/analysis")),
    retry: false,
  });
}

/** Choose the limits, in place of the choice before. */
export function useSetAnalysisLimits() {
  const queryClient = useQueryClient();
  return useMutation<AnalysisSettings, ApiError, AnalysisLimitsRequest>({
    mutationFn: async (limits) => dataOf(await api.PUT("/v1/settings/analysis", { body: limits })),
    onSuccess: (settings) => queryClient.setQueryData(queryKeys.analysisSettings(), settings),
  });
}

/** Go back to the analyzer's defaults. */
export function useRemoveAnalysisLimits() {
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, void>({
    mutationFn: async () => doneOf(await api.DELETE("/v1/settings/analysis")),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.analysisSettings() }),
  });
}
