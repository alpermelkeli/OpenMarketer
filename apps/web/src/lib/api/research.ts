"use client";

/** Query hooks for research runs: the project's runs, one run, and the report of a finished one. */

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { isRunFinished } from "@/lib/analysis/run-status";

import type { ApiError } from "./api-error";
import { api, dataOf } from "./client";
import { PAGE_SIZE, mergePages, nextCursor } from "./pages";
import { queryKeys } from "./query-keys";
import type { ResearchReport, ResearchRun, ResearchRunList } from "./types";

const POLL_INTERVAL_MS = 2_000;

/** A project's research runs, newest first, loaded a page at a time. */
export function useResearches(projectId: string) {
  return useInfiniteQuery<
    ResearchRunList,
    ApiError,
    ResearchRun[],
    ReturnType<typeof queryKeys.researches>,
    string | undefined
  >({
    queryKey: queryKeys.researches(projectId),
    queryFn: async ({ pageParam }) =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/researches", {
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

/**
 * One run, polled until it has finished. `listed` is the run as a list gave it, so a screen
 * that already has it shows it at once; a run's own screen reads it by its id.
 */
export function useResearchRun(projectId: string, runId: string, listed?: ResearchRun) {
  return useQuery<ResearchRun, ApiError>({
    queryKey: queryKeys.researchRun(projectId, runId),
    queryFn: async () =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/researches/{run_id}", {
          params: { path: { project_id: projectId, run_id: runId } },
        }),
      ),
    initialData: listed,
    enabled: listed === undefined || !isRunFinished(listed),
    refetchInterval: (query) => {
      const run = query.state.data;
      return run !== undefined && isRunFinished(run) ? false : POLL_INTERVAL_MS;
    },
    staleTime: 0,
    retry: false,
  });
}

/** The report of a run that succeeded; not asked for while the run has none. */
export function useResearchReport(projectId: string, runId: string, enabled: boolean) {
  return useQuery<ResearchReport, ApiError>({
    queryKey: queryKeys.researchReport(projectId, runId),
    queryFn: async () =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/researches/{run_id}/report", {
          params: { path: { project_id: projectId, run_id: runId } },
        }),
      ),
    enabled,
    staleTime: Infinity,
    retry: false,
  });
}
