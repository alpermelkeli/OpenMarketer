"use client";

/**
 * The query hook for the images the last analysis found in a project's repository. An image's
 * bytes are not read here: the page shows each from its own address (`lib/studio/repository-images.ts`).
 * Nothing polls the list: an analysis that ends refreshes it with the rest of the project.
 */

import { useInfiniteQuery } from "@tanstack/react-query";

import type { ApiError } from "./api-error";
import { api, dataOf } from "./client";
import { PAGE_SIZE, mergePages, nextCursor } from "./pages";
import { queryKeys } from "./query-keys";
import type { RepositoryImage, RepositoryImageList } from "./types";

/** A project's repository images, loaded a page at a time. */
export function useRepositoryImages(projectId: string) {
  return useInfiniteQuery<
    RepositoryImageList,
    ApiError,
    RepositoryImage[],
    ReturnType<typeof queryKeys.repositoryImages>,
    string | undefined
  >({
    queryKey: queryKeys.repositoryImages(projectId),
    queryFn: async ({ pageParam }) =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/repository-images", {
          params: {
            path: { project_id: projectId },
            query: { limit: PAGE_SIZE, cursor: pageParam },
          },
        }),
      ),
    initialPageParam: undefined,
    getNextPageParam: nextCursor,
    select: (data) =>
      mergePages(
        data.pages.map((page) => page.images),
        (image) => image.id,
      ),
    retry: false,
  });
}
