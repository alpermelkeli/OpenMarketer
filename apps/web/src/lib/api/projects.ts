"use client";

/** Query and mutation hooks for projects. */

import { type InfiniteData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { ApiError } from "./api-error";
import { api, dataOf, doneOf } from "./client";
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

/** The project as the sidebar shows it, under a key of its own (see `queryKeys.shellProject`). */
export function useShellProject(projectId: string) {
  return useQuery<Project, ApiError>({
    queryKey: queryKeys.shellProject(projectId),
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

/**
 * Delete a project for good, with its analyses and every profile version. The list
 * drops it at once; what was held about the project itself is left for the screen
 * that showed it to leave, so that screen does not ask for a project that is gone.
 */
export function useDeleteProject(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, void>({
    mutationFn: async () =>
      doneOf(await api.DELETE("/v1/projects/{project_id}", { params: { path: { project_id: projectId } } })),
    onSuccess: () => {
      queryClient.setQueryData<InfiniteData<ProjectList, string | undefined>>(queryKeys.projects(), (list) =>
        list === undefined
          ? list
          : {
              ...list,
              pages: list.pages.map((page) => ({
                ...page,
                projects: page.projects.filter((project) => project.id !== projectId),
              })),
            },
      );
      return queryClient.invalidateQueries({ queryKey: queryKeys.projects() });
    },
    // A 409 means a run started since the project was read; show it.
    onError: () => queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) }),
  });
}
