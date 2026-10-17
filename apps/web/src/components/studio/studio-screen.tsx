"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";

import { LoadMore } from "@/components/common/load-more";
import { RequestProblem } from "@/components/common/request-problem";
import { Skeleton } from "@/components/ui/skeleton";
import { useMediaAssets, useStartStudioRun, useStudioRuns } from "@/lib/api/studio";
import { projectPageHref, studioHref } from "@/lib/projects/project-navigation";
import { libraryTiles } from "@/lib/studio/library";
import { studioRequestProblems } from "@/lib/studio/request-form";

import { LibraryGrid } from "./library-grid";
import { NewImageForm } from "./new-image-form";

type StudioScreenProps = { projectId: string };

/** The project's studio: the images its runs made, each with where it stands, and a way to ask for one. */
export function StudioScreen({ projectId }: StudioScreenProps) {
  const router = useRouter();
  const runs = useStudioRuns(projectId);
  const assets = useMediaAssets(projectId);
  const start = useStartStudioRun(projectId);
  const error = runs.error ?? assets.error;
  const tiles = runs.data === undefined || assets.data === undefined ? undefined : libraryTiles(runs.data, assets.data);

  return (
    <div className="grid gap-8">
      <section aria-labelledby="about-studio" className="max-w-prose">
        <h2 id="about-studio" className="font-heading text-title">
          Make images for the product
        </h2>
        <p className="mt-2 text-pretty text-muted-foreground">
          A studio run makes one image for one platform. It plans the image, makes candidates, checks their size and
          words in code, has a second model look at them, and edits until one passes. What it ends with is a draft:
          nothing is used anywhere until you accept it. Ask for one from{" "}
          <Link
            href={projectPageHref(projectId, "chat")}
            className="rounded-sm text-brand underline-offset-4 hover:underline"
          >
            a chat with the orchestrator
          </Link>
          , or below.
        </p>
      </section>

      <section aria-labelledby="library" className="grid gap-4 border-t pt-8">
        <h2 id="library" className="font-heading text-lg">
          Library
        </h2>
        {error !== null && (
          <RequestProblem
            title="The library could not be read"
            error={error}
            onRetry={() => {
              void runs.refetch();
              void assets.refetch();
            }}
          />
        )}
        {tiles === undefined && error === null && <Skeleton className="h-48" />}
        {tiles?.length === 0 && (
          <p className="max-w-prose text-sm text-pretty text-muted-foreground">
            No image has been made for this project yet. Ask the orchestrator for an image in a chat, or start a run
            below.
          </p>
        )}
        {tiles !== undefined && tiles.length > 0 && <LibraryGrid projectId={projectId} tiles={tiles} />}
        <LoadMore
          noun="images"
          hasMore={runs.hasNextPage || assets.hasNextPage}
          loading={runs.isFetchingNextPage || assets.isFetchingNextPage}
          onLoadMore={() => {
            if (runs.hasNextPage) void runs.fetchNextPage();
            if (assets.hasNextPage) void assets.fetchNextPage();
          }}
        />
      </section>

      <section aria-labelledby="new-image" className="grid gap-4 border-t pt-8">
        <h2 id="new-image" className="font-heading text-lg">
          New image
        </h2>
        <NewImageForm
          problems={studioRequestProblems(start.error)}
          pending={start.isPending}
          onSubmit={(request) =>
            start.mutate(request, { onSuccess: (run) => router.push(studioHref(projectId, run.id)) })
          }
        />
      </section>
    </div>
  );
}
