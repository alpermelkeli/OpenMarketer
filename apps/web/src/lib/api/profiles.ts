"use client";

/** Query and mutation hooks for reviewing a project's Product Profile. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError } from "./api-error";
import { api, dataOf } from "./client";
import { queryKeys } from "./query-keys";
import type { ProductProfile, ProfileVersion } from "./types";

/** The latest draft, or `null` when the project has none. */
export function useDraftProfile(projectId: string) {
  return useQuery<ProfileVersion | null, ApiError>({
    queryKey: queryKeys.draftProfile(projectId),
    queryFn: async () =>
      orNoneIfMissing(async () =>
        dataOf(
          await api.GET("/v1/projects/{project_id}/profile/draft", {
            params: { path: { project_id: projectId } },
          }),
        ),
      ),
    retry: false,
  });
}

/** The latest approved version, or `null` when nothing has been approved. */
export function useApprovedProfile(projectId: string) {
  return useQuery<ProfileVersion | null, ApiError>({
    queryKey: queryKeys.approvedProfile(projectId),
    queryFn: async () =>
      orNoneIfMissing(async () =>
        dataOf(
          await api.GET("/v1/projects/{project_id}/profile/approved", {
            params: { path: { project_id: projectId } },
          }),
        ),
      ),
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
      // A new version always has the highest number, so it is the latest draft.
      queryClient.setQueryData(queryKeys.draftProfile(projectId), newDraft);
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
    // Which version is now the latest draft and the latest approved is the server's
    // to say, after a refusal as much as after a success.
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.profile(projectId) }),
  });
}

async function orNoneIfMissing(read: () => Promise<ProfileVersion>): Promise<ProfileVersion | null> {
  try {
    return await read();
  } catch (error) {
    if (error instanceof ApiError && error.code === "profile_not_found") return null;
    throw error;
  }
}
