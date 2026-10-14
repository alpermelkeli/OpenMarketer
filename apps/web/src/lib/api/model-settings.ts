"use client";

/**
 * Query and mutation hooks for the workspace's models: the settings, the provider's
 * catalogue and the check of each role's models against it.
 *
 * A write answers with the whole of the settings, since a tier's model moves every role
 * on it; it is put in the cache as it is, and the check is asked for again.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { ApiError } from "./api-error";
import { api, dataOf, doneOf } from "./client";
import { queryKeys } from "./query-keys";
import type { ModelCatalog, ModelCheck, ModelSettings, RejectableParameter, RoleChoiceRequest } from "./types";

/** Every tier and role of the configuration, what it resolves to and where that comes from. */
export function useModelSettings() {
  return useQuery<ModelSettings, ApiError>({
    queryKey: queryKeys.modelSettings(),
    queryFn: async () => dataOf(await api.GET("/v1/settings/models")),
    retry: false,
  });
}

/** The provider's models, for the picker. Read through the API, and kept for a while. */
export function useModelCatalog() {
  return useQuery<ModelCatalog, ApiError>({
    queryKey: queryKeys.modelCatalog(),
    queryFn: async () => dataOf(await api.GET("/v1/settings/models/catalog")),
    staleTime: 10 * 60 * 1000,
    retry: false,
  });
}

/** What each role's models lack for the role, according to the catalogue. */
export function useModelCheck() {
  return useQuery<ModelCheck, ApiError>({
    queryKey: queryKeys.modelCheck(),
    queryFn: async () => dataOf(await api.GET("/v1/settings/models/check")),
    retry: false,
  });
}

function useSettingsWrite<Variables>(write: (variables: Variables) => Promise<ModelSettings>) {
  const queryClient = useQueryClient();
  return useMutation<ModelSettings, ApiError, Variables>({
    mutationFn: write,
    onSuccess: (settings) => queryClient.setQueryData(queryKeys.modelSettings(), settings),
    // After a refusal too: a 409 means the environment now sets what the screen offered.
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.modelScope() }),
  });
}

function useSettingsRemoval(remove: () => Promise<void>) {
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, void>({
    mutationFn: remove,
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.modelScope() }),
  });
}

/** Choose the model of a tier. */
export function useSetTierModel(tier: string) {
  return useSettingsWrite<string>(async (model) =>
    dataOf(await api.PUT("/v1/settings/models/tiers/{tier}", { params: { path: { tier } }, body: { model } })),
  );
}

/** Go back to the configuration file's model for a tier. */
export function useRemoveTierModel(tier: string) {
  return useSettingsRemoval(async () =>
    doneOf(await api.DELETE("/v1/settings/models/tiers/{tier}", { params: { path: { tier } } })),
  );
}

/** Choose a role's model and parameters, in place of the choice before. */
export function useSetRoleChoice(role: string) {
  return useSettingsWrite<RoleChoiceRequest>(async (choice) =>
    dataOf(await api.PUT("/v1/settings/models/roles/{role}", { params: { path: { role } }, body: choice })),
  );
}

/** Go back to the configuration file for every part of a role. */
export function useRemoveRoleChoice(role: string) {
  return useSettingsRemoval(async () =>
    doneOf(await api.DELETE("/v1/settings/models/roles/{role}", { params: { path: { role } } })),
  );
}

export type RejectedParametersChoice = { model: string; parameters: RejectableParameter[] };

/** Mark a model as rejecting parameters; none removes the mark. */
export function useSetRejectedParameters() {
  return useSettingsWrite<RejectedParametersChoice>(async (choice) =>
    dataOf(await api.PUT("/v1/settings/models/rejected-parameters", { body: choice })),
  );
}
