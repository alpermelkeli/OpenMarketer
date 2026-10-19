import Link from "next/link";

import { LoadMore } from "@/components/common/load-more";
import { RequestProblem } from "@/components/common/request-problem";
import { Skeleton } from "@/components/ui/skeleton";
import type { ApiError } from "@/lib/api/api-error";
import type { RepositoryImage } from "@/lib/api/types";
import { projectPageHref } from "@/lib/projects/project-navigation";

import { AddRepositoryImage } from "./add-repository-image";
import { RepositoryImageGrid } from "./repository-image-grid";

type RepositoryImagesProps = {
  projectId: string;
  /** Undefined until the first page has been read. */
  images: readonly RepositoryImage[] | undefined;
  error: ApiError | null;
  onRetry: () => void;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
};

/**
 * The section of the project's repository images: those the last analysis found and those a person
 * added, which the studio may use as references, with the way to add, relabel and remove them.
 */
export function RepositoryImages({
  projectId,
  images,
  error,
  onRetry,
  hasMore,
  loadingMore,
  onLoadMore,
}: RepositoryImagesProps) {
  return (
    <section
      aria-labelledby="repository-images"
      className="grid gap-4 border-t pt-8"
    >
      <div className="grid max-w-prose gap-1">
        <h2 id="repository-images" className="font-heading text-lg">
          From the repository
        </h2>
        <p className="text-sm text-pretty text-muted-foreground">
          Images the last analysis found in the repository, and any you added.
          The studio uses what is listed here as references. Add an image,
          change an image&rsquo;s kind or remove one here; run an analysis
          again to refresh the ones it finds.
        </p>
      </div>
      {error !== null && (
        <RequestProblem
          title="The repository's images could not be read"
          error={error}
          onRetry={onRetry}
        />
      )}
      {images === undefined && error === null && <Skeleton className="h-40" />}
      {images?.length === 0 && (
        <p className="max-w-prose text-sm text-pretty text-muted-foreground">
          No image is listed: the last analysis found none in the repository,
          or none has run since images are collected, and none was added.{" "}
          <Link
            href={projectPageHref(projectId, "analyses")}
            className="rounded-sm text-brand underline-offset-4 hover:underline"
          >
            Run an analysis
          </Link>{" "}
          to collect them, or add one below.
        </p>
      )}
      {images !== undefined && images.length > 0 && (
        <RepositoryImageGrid projectId={projectId} images={images} />
      )}
      <LoadMore
        noun="images"
        hasMore={hasMore}
        loading={loadingMore}
        onLoadMore={onLoadMore}
      />
      <AddRepositoryImage projectId={projectId} />
    </section>
  );
}
