"use client";

/**
 * Query hooks for the writer: the project's writing runs and one run, the posts they proposed and
 * one post with the record of how it was written, and the request that starts a run. Nothing here
 * decides on a post: that is done on its proposal (`proposals.ts`).
 */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { isWritingRunFinished } from "@/lib/writing/status";

import type { ApiError } from "./api-error";
import { api, dataOf } from "./client";
import { PAGE_SIZE, mergePages, nextCursor } from "./pages";
import { queryKeys, refreshedByWritingRunEnd } from "./query-keys";
import type {
  ContentItem,
  ContentItemDetail,
  ContentItemList,
  WritingBrief,
  WritingRun,
  WritingRunDetail,
  WritingRunList,
} from "./types";

const POLL_INTERVAL_MS = 2_000;

/** A project's writing runs, newest first, loaded a page at a time. */
export function useWritingRuns(projectId: string) {
  return useInfiniteQuery<
    WritingRunList,
    ApiError,
    WritingRun[],
    ReturnType<typeof queryKeys.writingRuns>,
    string | undefined
  >({
    queryKey: queryKeys.writingRuns(projectId),
    queryFn: async ({ pageParam }) =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/writing-runs", {
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
 * One run, polled until it has ended. `listed` is the run as a list gave it, so a card shows it at
 * once; the run's own route also serves its brief (`WritingRunDetail`), which a listed run lacks
 * until that route has answered. A run waits on its post's proposal while a person decides, so it
 * stays unfinished, and polled, until the decision is carried out. When this screen sees the run
 * end, the lists and the post it settled are read again.
 */
export function useWritingRun(projectId: string, runId: string, listed?: WritingRun) {
  const queryClient = useQueryClient();
  const query = useQuery<WritingRun | WritingRunDetail, ApiError>({
    queryKey: queryKeys.writingRun(projectId, runId),
    queryFn: async () =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/writing-runs/{run_id}", {
          params: { path: { project_id: projectId, run_id: runId } },
        }),
      ),
    initialData: listed,
    enabled: listed === undefined || !isWritingRunFinished(listed),
    refetchInterval: (current) => {
      const run = current.state.data;
      return run !== undefined && isWritingRunFinished(run) ? false : POLL_INTERVAL_MS;
    },
    staleTime: 0,
    retry: false,
  });

  // Only an end this screen watched happen: a run read already finished has changed nothing since.
  const seenUnfinished = useRef(false);
  const run = query.data;
  useEffect(() => {
    if (run === undefined) return;
    if (!isWritingRunFinished(run)) {
      seenUnfinished.current = true;
      return;
    }
    if (!seenUnfinished.current) return;
    seenUnfinished.current = false;
    for (const refresh of refreshedByWritingRunEnd(projectId, run.content_item_id)) {
      void queryClient.invalidateQueries(refresh);
    }
  }, [run, projectId, queryClient]);

  return query;
}

/** The posts the project's writing runs proposed, newest first, loaded a page at a time. */
export function useContentItems(projectId: string) {
  return useInfiniteQuery<
    ContentItemList,
    ApiError,
    ContentItem[],
    ReturnType<typeof queryKeys.contentItems>,
    string | undefined
  >({
    queryKey: queryKeys.contentItems(projectId),
    queryFn: async ({ pageParam }) =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/content-items", {
          params: { path: { project_id: projectId }, query: { limit: PAGE_SIZE, cursor: pageParam } },
        }),
      ),
    initialPageParam: undefined,
    getNextPageParam: nextCursor,
    select: (data) =>
      mergePages(
        data.pages.map((page) => page.items),
        (item) => item.id,
      ),
    retry: false,
  });
}

/** One post with the record of how it was written; not asked for until the run names its post. */
export function useContentItem(projectId: string, itemId: string | null) {
  return useQuery<ContentItemDetail, ApiError>({
    queryKey: queryKeys.contentItem(projectId, itemId ?? ""),
    queryFn: async () =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/content-items/{item_id}", {
          params: { path: { project_id: projectId, item_id: itemId ?? "" } },
        }),
      ),
    enabled: itemId !== null,
    retry: false,
  });
}

/** Queue a run for the brief. The run is held at once; the list is read again. */
export function useStartWritingRun(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<WritingRun, ApiError, WritingBrief>({
    mutationFn: async (body) =>
      dataOf(
        await api.POST("/v1/projects/{project_id}/writing-runs", {
          params: { path: { project_id: projectId } },
          body,
        }),
      ),
    onSuccess: (run) => {
      queryClient.setQueryData(queryKeys.writingRun(projectId, run.id), run);
      return queryClient.invalidateQueries({ queryKey: queryKeys.writingRuns(projectId) });
    },
  });
}
