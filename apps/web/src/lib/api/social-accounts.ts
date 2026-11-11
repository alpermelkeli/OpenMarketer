"use client";

/**
 * Query and mutation hooks for a project's connected accounts on social platforms.
 *
 * No response holds an account's token. What the platform sends back when a person agrees
 * (the connection's state and grant) travels the other way, in the body of the callback,
 * and is handed to the mutation as `OneTimeValue`s so that it is not left in the cache.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { ApiError } from "./api-error";
import { api, dataOf, doneOf } from "./client";
import type { OneTimeValue } from "./one-time-value";
import { queryKeys } from "./query-keys";
import type { ConnectionCallbackResult, ConnectionStart, SocialAccounts, SocialPlatform } from "./types";

/** The project's connected accounts, every platform one can be connected on, and whether one can be. */
export function useSocialAccounts(projectId: string) {
  return useQuery<SocialAccounts, ApiError>({
    queryKey: queryKeys.socialAccounts(projectId),
    queryFn: async () =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/social-accounts", { params: { path: { project_id: projectId } } }),
      ),
    retry: false,
  });
}

/** Start connecting the project's account on a platform: the answer is the platform's page to send the person to. */
export function useConnectSocialAccount(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<ConnectionStart, ApiError, SocialPlatform>({
    mutationFn: async (platform) =>
      dataOf(
        await api.POST("/v1/projects/{project_id}/social-accounts/{platform}/connect", {
          params: { path: { project_id: projectId, platform } },
        }),
      ),
    // After a refusal too: a 409 means the app's credentials or the encryption key are not what the screen said.
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.socialAccounts(projectId) }),
  });
}

/** Forget the project's account on a platform, and its token. The platform itself is not told. */
export function useDisconnectSocialAccount(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, SocialPlatform>({
    mutationFn: async (platform) =>
      doneOf(
        await api.DELETE("/v1/projects/{project_id}/social-accounts/{platform}", {
          params: { path: { project_id: projectId, platform } },
        }),
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.socialAccounts(projectId) }),
  });
}

/** What the platform sent back: the connection it names, and the grant when the person agreed. */
export type ConnectionCallback = { state: OneTimeValue; grant: OneTimeValue | null };

/**
 * Finish the connection the platform answered, or forget it when the person declined. The API
 * says which project it was for, and that project's accounts are read again.
 */
export function useFinishConnection(platform: SocialPlatform) {
  const queryClient = useQueryClient();
  return useMutation<ConnectionCallbackResult, ApiError, ConnectionCallback>({
    mutationFn: async ({ state, grant }) =>
      dataOf(
        await api.POST("/v1/social-accounts/{platform}/callback", {
          params: { path: { platform } },
          body: grant === null ? { state: state.take() } : { state: state.take(), grant: grant.take() },
        }),
      ),
    // The finished mutation is dropped as soon as nothing shows it any more.
    gcTime: 0,
    onSuccess: (result) => queryClient.invalidateQueries({ queryKey: queryKeys.socialAccounts(result.project_id) }),
  });
}
