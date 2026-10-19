"use client";

/**
 * The hooks for a project's repository images: the list of what the last analysis found and what
 * a person added, adding one, choosing an image's kind and removing one. An image's bytes are not
 * read here: the page shows each from its own address (`lib/studio/repository-images.ts`). Nothing
 * polls the list: an analysis that ends refreshes it with the rest of the project, and each change
 * here refreshes it as `query-keys.ts` decides.
 */

import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";

import type { ApiError } from "./api-error";
import { api, dataOf, doneOf } from "./client";
import { PAGE_SIZE, mergePages, nextCursor } from "./pages";
import { queryKeys, refreshedByRepositoryImageChange } from "./query-keys";
import type {
  RepositoryImage,
  RepositoryImageKind,
  RepositoryImageList,
  RepositoryImageUpload,
} from "./types";

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

function useRefreshRepositoryImages(projectId: string) {
  const queryClient = useQueryClient();
  return () =>
    Promise.all(
      refreshedByRepositoryImageChange(projectId).map((refresh) => queryClient.invalidateQueries(refresh)),
    );
}

/**
 * Add an image to the project. The upload holds the whole file in base64, so it is dropped from
 * the mutation cache as soon as nothing reads it (`gcTime: 0`). Sending the same bytes again
 * answers with the image already stored.
 */
export function useUploadRepositoryImage(projectId: string) {
  const refresh = useRefreshRepositoryImages(projectId);
  return useMutation<RepositoryImage, ApiError, RepositoryImageUpload>({
    mutationFn: async (body) =>
      dataOf(
        await api.POST("/v1/projects/{project_id}/repository-images", {
          params: { path: { project_id: projectId } },
          body,
        }),
      ),
    onSuccess: refresh,
    gcTime: 0,
  });
}

/** Choose an image's kind. The API keeps a person's choice over an analysis's hint. */
export function useSetRepositoryImageKind(projectId: string) {
  const refresh = useRefreshRepositoryImages(projectId);
  return useMutation<RepositoryImage, ApiError, { imageId: string; kind: RepositoryImageKind }>({
    mutationFn: async ({ imageId, kind }) =>
      dataOf(
        await api.PATCH("/v1/projects/{project_id}/repository-images/{image_id}", {
          params: { path: { project_id: projectId, image_id: imageId } },
          body: { kind },
        }),
      ),
    onSuccess: refresh,
  });
}

/** Remove an image: an upload is deleted, an image an analysis found is hidden from the studio. */
export function useRemoveRepositoryImage(projectId: string) {
  const refresh = useRefreshRepositoryImages(projectId);
  return useMutation<void, ApiError, string>({
    mutationFn: async (imageId) =>
      doneOf(
        await api.DELETE("/v1/projects/{project_id}/repository-images/{image_id}", {
          params: { path: { project_id: projectId, image_id: imageId } },
        }),
      ),
    onSuccess: refresh,
  });
}
