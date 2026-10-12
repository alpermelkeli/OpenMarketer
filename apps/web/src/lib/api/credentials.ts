"use client";

/**
 * Query and mutation hooks for the installation's credentials.
 *
 * The list says where each credential's value comes from; no response holds a value.
 * The one value there is travels the other way, in the body of `setCredential`, and
 * is handed to the mutation as a `OneTimeValue` so that it is not left in the cache.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { ApiError } from "./api-error";
import { api, dataOf, doneOf } from "./client";
import type { OneTimeValue } from "./one-time-value";
import { queryKeys } from "./query-keys";
import type { CredentialList, CredentialStatus } from "./types";

/** Every credential of the installation, in the API's order, and whether one can be stored. */
export function useCredentials() {
  return useQuery<CredentialList, ApiError>({
    queryKey: queryKeys.credentials(),
    queryFn: async () => dataOf(await api.GET("/v1/settings/credentials")),
    retry: false,
  });
}

/** Store a value for one credential, in place of the one stored before. */
export function useSetCredential(name: string) {
  const queryClient = useQueryClient();
  return useMutation<CredentialStatus, ApiError, OneTimeValue>({
    mutationFn: async (value) =>
      dataOf(
        await api.PUT("/v1/settings/credentials/{name}", {
          params: { path: { name } },
          body: { value: value.take() },
        }),
      ),
    // The finished mutation is dropped as soon as nothing shows it any more.
    gcTime: 0,
    onSuccess: (stored) => {
      queryClient.setQueryData<CredentialList>(queryKeys.credentials(), (list) =>
        list === undefined
          ? list
          : { ...list, credentials: list.credentials.map((known) => (known.name === stored.name ? stored : known)) },
      );
    },
    // After a refusal too: a 409 means the environment now sets the credential, or the
    // API has no encryption key, and the list on screen said otherwise.
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.credentials() }),
  });
}

/** Remove the stored value of one credential. A value from the environment is not touched. */
export function useRemoveCredential(name: string) {
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, void>({
    mutationFn: async () =>
      doneOf(await api.DELETE("/v1/settings/credentials/{name}", { params: { path: { name } } })),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.credentials() }),
  });
}
