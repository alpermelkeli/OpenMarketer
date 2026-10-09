"use client";

/** Mutation hook for projects. The API cannot list or read projects yet, so there is no query. */

import { useMutation } from "@tanstack/react-query";

import type { ApiError } from "./api-error";
import { api, dataOf } from "./client";
import type { CreateProjectRequest, Project } from "./types";

export function useCreateProject() {
  return useMutation<Project, ApiError, CreateProjectRequest>({
    mutationFn: async (body) => dataOf(await api.POST("/v1/projects", { body })),
  });
}
