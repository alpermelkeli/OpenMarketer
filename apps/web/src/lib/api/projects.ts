"use client";

/** Query and mutation hooks for projects. */

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { ApiError } from "./api-error";
import { api, dataOf } from "./client";
import { PAGE_SIZE, mergePages, nextCursor } from "./pages";
import { queryKeys } from "./query-keys";
import type { CreateProjectRequest, Project, ProjectList } from "./types";

/** Every project of the workspace, newest first, loaded a page at a time. */
export function useProjects() {
  return useInfiniteQuery<ProjectList, ApiError, Project[], ReturnType<typeof queryKeys.projects>, string | undefined>({
    queryKey: queryKeys.projects(),
    queryFn: async ({ pageParam }) =>
      dataOf(await api.GET("/v1/projects", { params: { query: { limit: PAGE_SIZE, cursor: pageParam } } })),
    initialPageParam: undefined,
    getNextPageParam: nextCursor,
    select: (data) =>
      mergePages(
        data.pages.map((page) => page.projects),
        (project) => project.id,
      ),
  });
}

export function useProject(projectId: string) {
  return useQuery<Project, ApiError>({
    queryKey: queryKeys.project(projectId),
    queryFn: async () =>
      dataOf(await api.GET("/v1/projects/{project_id}", { params: { path: { project_id: projectId } } })),
    retry: false,
  });
}

export function useCreateProject() {
  const queryClient = useQueryClient();
  return useMutation<Project, ApiError, CreateProjectRequest>({
    mutationFn: async (body) => dataOf(await api.POST("/v1/projects", { body })),
    onSuccess: (project) => {
      queryClient.setQueryData(queryKeys.project(project.id), project);
      return queryClient.invalidateQueries({ queryKey: queryKeys.projects() });
    },
  });
}
