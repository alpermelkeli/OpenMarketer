"use client";

/** Query and mutation hooks for reviewing a project's Product Profile. */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { ApiError } from "./api-error";
import { api, dataOf } from "./client";
import { PAGE_SIZE, mergePages, nextCursor } from "./pages";
import { queryKeys } from "./query-keys";
import type { ProductProfile, ProfileVersion, ProfileVersionList, ProfileVersionSummary } from "./types";

/** A project's profile versions without their content, newest first, a page at a time. */
export function useProfileVersions(projectId: string) {
  return useInfiniteQuery<
    ProfileVersionList,
    ApiError,
    ProfileVersionSummary[],
    ReturnType<typeof queryKeys.profileVersions>,
    string | undefined
  >({
    queryKey: queryKeys.profileVersions(projectId),
    queryFn: async ({ pageParam }) =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/profile/versions", {
          params: { path: { project_id: projectId }, query: { limit: PAGE_SIZE, cursor: pageParam } },
        }),
      ),
    initialPageParam: undefined,
    getNextPageParam: nextCursor,
    select: (data) =>
      mergePages(
        data.pages.map((page) => page.versions),
        (version) => version.version,
      ),
    retry: false,
  });
}

/** One version with its content. `version` may be absent while the screen works out which to show. */
export function useProfileVersion(projectId: string, version: number | null) {
  return useQuery<ProfileVersion, ApiError>({
    queryKey: queryKeys.profileVersion(projectId, version ?? 0),
    queryFn: async () =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/profile/versions/{version}", {
          params: { path: { project_id: projectId, version: version ?? 0 } },
        }),
      ),
    enabled: version !== null,
    retry: false,
  });
}

export type ProfileEdit = {
  /** The version the edit starts from. It is left as it is. */
  version: number;
  profile: ProductProfile;
};

/** Store an edit as a new draft version. */
export function useSaveProfileEdit(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<ProfileVersion, ApiError, ProfileEdit>({
    mutationFn: async ({ version, profile }) =>
      dataOf(
        await api.POST("/v1/projects/{project_id}/profile/versions/{version}/edits", {
          params: { path: { project_id: projectId, version } },
          body: { profile },
        }),
      ),
    onSuccess: (newDraft) => {
      queryClient.setQueryData(queryKeys.profileVersion(projectId, newDraft.version), newDraft);
      void queryClient.invalidateQueries({ queryKey: queryKeys.profileVersions(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
    },
  });
}

/** Approve one version. Permanent: the API has no way to undo it. */
export function useApproveProfileVersion(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<ProfileVersion, ApiError, number>({
    mutationFn: async (version) =>
      dataOf(
        await api.POST("/v1/projects/{project_id}/profile/versions/{version}/approval", {
          params: { path: { project_id: projectId, version } },
        }),
      ),
    // What is approved now is the server's to say, after a refusal as much as after a success.
    onSettled: (_approved, _error, version) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.profileVersion(projectId, version) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.profileVersions(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.projects() });
    },
  });
}
