"use client";

/** Query and mutation hooks for the creative budget: what generated media may cost, and what it has. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { ApiError } from "./api-error";
import { api, dataOf, doneOf } from "./client";
import { queryKeys } from "./query-keys";
import type { CreativeBudget, CreativeBudgetRequest } from "./types";

/** The caps in use, where each comes from, and this month's spending. */
export function useCreativeBudget() {
  return useQuery<CreativeBudget, ApiError>({
    queryKey: queryKeys.creativeBudget(),
    queryFn: async () => dataOf(await api.GET("/v1/settings/creative-budget")),
    retry: false,
  });
}

/** Choose the caps, in place of the choice before. */
export function useSetCreativeBudget() {
  const queryClient = useQueryClient();
  return useMutation<CreativeBudget, ApiError, CreativeBudgetRequest>({
    mutationFn: async (caps) => dataOf(await api.PUT("/v1/settings/creative-budget", { body: caps })),
    onSuccess: (budget) => queryClient.setQueryData(queryKeys.creativeBudget(), budget),
  });
}

/** Go back to the environment's caps and the defaults. */
export function useRemoveCreativeBudget() {
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, void>({
    mutationFn: async () => doneOf(await api.DELETE("/v1/settings/creative-budget")),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.creativeBudget() }),
  });
}
