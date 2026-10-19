import Link from "next/link";

import { LoadMore } from "@/components/common/load-more";
import { RequestProblem } from "@/components/common/request-problem";
import { Skeleton } from "@/components/ui/skeleton";
import type { ApiError } from "@/lib/api/api-error";
import type { RepositoryImage } from "@/lib/api/types";
import { projectPageHref } from "@/lib/projects/project-navigation";

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

/** The section of the images the last analysis found in the repository, which the studio may use as references. */
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
          Images the last analysis found in the repository. The studio may use
          them as references; run an analysis again to refresh them.
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
          The last analysis found no images in the repository, or none has run
          since images are collected.{" "}
          <Link
            href={projectPageHref(projectId, "analyses")}
            className="rounded-sm text-brand underline-offset-4 hover:underline"
          >
            Run an analysis
          </Link>{" "}
          to collect them.
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
    </section>
  );
}
