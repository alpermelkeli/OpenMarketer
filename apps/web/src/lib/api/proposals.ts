"use client";

/** Query and mutation hooks for the proposals a project's agents wait on: actions held for a person. */

import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import type { ApiError } from "./api-error";
import { api, dataOf } from "./client";
import { PAGE_SIZE, mergePages, nextCursor } from "./pages";
import { queryKeys } from "./query-keys";
import type { Proposal, ProposalList } from "./types";

/** A project's proposals, newest first, loaded a page at a time. */
export function useProposals(projectId: string) {
  return useInfiniteQuery<
    ProposalList,
    ApiError,
    Proposal[],
    ReturnType<typeof queryKeys.proposals>,
    string | undefined
  >({
    queryKey: queryKeys.proposals(projectId),
    queryFn: async ({ pageParam }) =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/proposals", {
          params: { path: { project_id: projectId }, query: { limit: PAGE_SIZE, cursor: pageParam } },
        }),
      ),
    initialPageParam: undefined,
    getNextPageParam: nextCursor,
    select: (data) =>
      mergePages(
        data.pages.map((page) => page.proposals),
        (proposal) => proposal.id,
      ),
    retry: false,
  });
}

export type Decision = { proposalId: string; approved: boolean };

/** Approve or reject one proposal as the current user. The run that waits for it is told by the API. */
export function useDecideProposal(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<Proposal, ApiError, Decision>({
    mutationFn: async ({ proposalId, approved }) => {
      const params = { path: { project_id: projectId, proposal_id: proposalId } };
      return dataOf(
        approved
          ? await api.POST("/v1/projects/{project_id}/proposals/{proposal_id}/approval", { params })
          : await api.POST("/v1/projects/{project_id}/proposals/{proposal_id}/rejection", { params }),
      );
    },
    // Where the proposal stands is the server's to say, after a refusal as much as after a success.
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.proposals(projectId) }),
  });
}
