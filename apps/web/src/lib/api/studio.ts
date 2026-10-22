"use client";

/**
 * Query hooks for the creative studio: the project's runs and one run, its library of assets
 * and one asset with its versions, and the requests that start a run, decide on an asset,
 * download a version's image and delete a run. A version's image is not read here to be shown:
 * the page shows it from its own address (`lib/studio/image-src.ts`).
 */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { saveFile, versionFileName } from "@/lib/studio/download";
import { isStudioRunFinished } from "@/lib/studio/status";

import type { ApiError } from "./api-error";
import { api, dataOf, doneOf } from "./client";
import { PAGE_SIZE, mergePages, nextCursor } from "./pages";
import { queryKeys, refreshedByAssetDecision, refreshedByStudioRunDeleted, refreshedByStudioRunEnd } from "./query-keys";
import type {
  AssetRequest,
  MediaAsset,
  MediaAssetDetail,
  MediaAssetList,
  MediaAssetVersion,
  StudioRun,
  StudioRunDetail,
  StudioRunList,
} from "./types";

const POLL_INTERVAL_MS = 2_000;

/** A project's studio runs, newest first, loaded a page at a time. */
export function useStudioRuns(projectId: string) {
  return useInfiniteQuery<
    StudioRunList,
    ApiError,
    StudioRun[],
    ReturnType<typeof queryKeys.studioRuns>,
    string | undefined
  >({
    queryKey: queryKeys.studioRuns(projectId),
    queryFn: async ({ pageParam }) =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/studio-runs", {
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
 * One run, polled until it has ended. `listed` is the run as a list gave it, so a tile shows it
 * at once; the run's own route also serves what it was asked for (`StudioRunDetail`), which a
 * listed run lacks until that route has answered. A screen that follows the run's events instead
 * (a chat) reads it once, without `poll`: the event that ends the run refreshes it. When this
 * screen sees the run end, the lists it changed are read again.
 */
export function useStudioRun(
  projectId: string,
  runId: string,
  listed?: StudioRun,
  { poll = true }: { poll?: boolean } = {},
) {
  const queryClient = useQueryClient();
  const query = useQuery<StudioRun | StudioRunDetail, ApiError>({
    queryKey: queryKeys.studioRun(projectId, runId),
    queryFn: async () =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/studio-runs/{run_id}", {
          params: { path: { project_id: projectId, run_id: runId } },
        }),
      ),
    initialData: listed,
    enabled: listed === undefined || !isStudioRunFinished(listed),
    refetchInterval: (current) => {
      if (!poll) return false;
      const run = current.state.data;
      return run !== undefined && isStudioRunFinished(run) ? false : POLL_INTERVAL_MS;
    },
    staleTime: 0,
    retry: false,
  });

  // Only an end this screen watched happen: a run read already finished has changed nothing since.
  const seenUnfinished = useRef(false);
  const run = query.data;
  useEffect(() => {
    if (run === undefined) return;
    if (!isStudioRunFinished(run)) {
      seenUnfinished.current = true;
      return;
    }
    if (!seenUnfinished.current) return;
    seenUnfinished.current = false;
    for (const refresh of refreshedByStudioRunEnd(projectId)) void queryClient.invalidateQueries(refresh);
  }, [run, projectId, queryClient]);

  return query;
}

/** A project's library: the assets its runs made, newest first, loaded a page at a time. */
export function useMediaAssets(projectId: string) {
  return useInfiniteQuery<
    MediaAssetList,
    ApiError,
    MediaAsset[],
    ReturnType<typeof queryKeys.mediaAssets>,
    string | undefined
  >({
    queryKey: queryKeys.mediaAssets(projectId),
    queryFn: async ({ pageParam }) =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/media-assets", {
          params: { path: { project_id: projectId }, query: { limit: PAGE_SIZE, cursor: pageParam } },
        }),
      ),
    initialPageParam: undefined,
    getNextPageParam: nextCursor,
    select: (data) =>
      mergePages(
        data.pages.map((page) => page.assets),
        (asset) => asset.id,
      ),
    retry: false,
  });
}

/** One asset with every version; not asked for until the run names its asset. */
export function useMediaAsset(projectId: string, assetId: string | null) {
  return useQuery<MediaAssetDetail, ApiError>({
    queryKey: queryKeys.mediaAsset(projectId, assetId ?? ""),
    queryFn: async () =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/media-assets/{asset_id}", {
          params: { path: { project_id: projectId, asset_id: assetId ?? "" } },
        }),
      ),
    enabled: assetId !== null,
    retry: false,
  });
}

/** Queue a run for the image asked for. The run is held at once; the list is read again. */
export function useStartStudioRun(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<StudioRun, ApiError, AssetRequest>({
    mutationFn: async (body) =>
      dataOf(
        await api.POST("/v1/projects/{project_id}/studio-runs", {
          params: { path: { project_id: projectId } },
          body,
        }),
      ),
    onSuccess: (run) => {
      queryClient.setQueryData(queryKeys.studioRun(projectId, run.id), run);
      return queryClient.invalidateQueries({ queryKey: queryKeys.studioRuns(projectId) });
    },
  });
}

type Decision = "accept" | "reject";

/**
 * Choose the version a draft asset stands for, before a decision. The asset is set from the
 * answer, so the large image and what Accept and Download use change with it; the library is
 * read again because its tile shows the current version.
 */
export function useSelectAssetVersion(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<MediaAsset, ApiError, { assetId: string; versionId: string }>({
    mutationFn: async ({ assetId, versionId }) =>
      dataOf(
        await api.POST(
          "/v1/projects/{project_id}/media-assets/{asset_id}/versions/{version_id}/select",
          { params: { path: { project_id: projectId, asset_id: assetId, version_id: versionId } } },
        ),
      ),
    onSuccess: (asset) => {
      queryClient.setQueryData<MediaAssetDetail>(queryKeys.mediaAsset(projectId, asset.id), (detail) =>
        detail === undefined ? detail : { ...detail, asset },
      );
      return Promise.all(
        refreshedByAssetDecision(projectId).map((refresh) => queryClient.invalidateQueries(refresh)),
      );
    },
  });
}

/** Accept or reject a draft asset as its current version stands. The asset is set from the answer. */
export function useDecideOnAsset(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<MediaAsset, ApiError, { assetId: string; decision: Decision }>({
    mutationFn: async ({ assetId, decision }) => {
      const params = { path: { project_id: projectId, asset_id: assetId } };
      return dataOf(
        decision === "accept"
          ? await api.POST("/v1/projects/{project_id}/media-assets/{asset_id}/accept", { params })
          : await api.POST("/v1/projects/{project_id}/media-assets/{asset_id}/reject", { params }),
      );
    },
    onSuccess: (asset) => {
      queryClient.setQueryData<MediaAssetDetail>(queryKeys.mediaAsset(projectId, asset.id), (detail) =>
        detail === undefined ? detail : { ...detail, asset },
      );
      return Promise.all(refreshedByAssetDecision(projectId).map((refresh) => queryClient.invalidateQueries(refresh)));
    },
  });
}

/**
 * Save one version's image as a file. The page fetches it through its own proxy and hands the
 * bytes to the browser, so nothing navigates to the proxy (which refuses a navigation it cannot
 * tell from another site's). Nothing in the cache changes.
 */
export function useDownloadVersion(projectId: string) {
  return useMutation<void, ApiError, { assetId: string; version: MediaAssetVersion }>({
    mutationFn: async ({ assetId, version }) => {
      const file = dataOf(
        await api.GET("/v1/projects/{project_id}/media-assets/{asset_id}/versions/{version_id}/download", {
          params: { path: { project_id: projectId, asset_id: assetId, version_id: version.id } },
          parseAs: "blob",
        }),
      );
      saveFile(file, versionFileName(version.id, version.mime || file.type));
    },
  });
}

/**
 * Delete a finished run for good, with its asset and every version's image. The run and its
 * asset are dropped at once; the lists are read again for the rest.
 */
export function useDeleteStudioRun(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, StudioRun>({
    mutationFn: async (run) =>
      doneOf(
        await api.DELETE("/v1/projects/{project_id}/studio-runs/{run_id}", {
          params: { path: { project_id: projectId, run_id: run.id } },
        }),
      ),
    onSuccess: (_deleted, run) => {
      queryClient.removeQueries({ queryKey: queryKeys.studioRun(projectId, run.id) });
      if (run.final_asset_id !== null) {
        queryClient.removeQueries({ queryKey: queryKeys.mediaAsset(projectId, run.final_asset_id) });
      }
      return Promise.all(
        refreshedByStudioRunDeleted(projectId).map((refresh) => queryClient.invalidateQueries(refresh)),
      );
    },
  });
}
