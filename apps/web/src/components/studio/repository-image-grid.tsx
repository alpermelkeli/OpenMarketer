import { ImageOffIcon } from "lucide-react";
import Image from "next/image";

import type { RepositoryImage } from "@/lib/api/types";
import {
  repositoryImageAlt,
  repositoryImageGroups,
  repositoryImageSize,
  repositoryImageSrc,
} from "@/lib/studio/repository-images";

type RepositoryImageGridProps = {
  projectId: string;
  images: readonly RepositoryImage[];
};

/** The repository's images in a quiet grid, one group per kind, each kind named in words. */
export function RepositoryImageGrid({
  projectId,
  images,
}: RepositoryImageGridProps) {
  return (
    <div className="grid gap-6">
      {repositoryImageGroups(images).map((group) => (
        <section
          key={group.kind}
          aria-labelledby={`repository-images-${group.kind}`}
          className="grid gap-3"
        >
          <h3
            id={`repository-images-${group.kind}`}
            className="text-2xs tracking-widest text-muted-foreground uppercase"
          >
            {group.label}
          </h3>
          <ul className="grid grid-cols-2 gap-x-4 gap-y-6 sm:grid-cols-3 lg:grid-cols-4">
            {group.images.map((image) => (
              <RepositoryImageTile
                key={image.id}
                projectId={projectId}
                image={image}
              />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

type RepositoryImageTileProps = { projectId: string; image: RepositoryImage };

/**
 * One image: its thumbnail, contained in a square, its path as text and its size. It opens at full
 * size in a new tab, from the address the dashboard builds for it.
 */
function RepositoryImageTile({ projectId, image }: RepositoryImageTileProps) {
  const src = repositoryImageSrc(projectId, image.id);
  const caption = (
    <>
      <span className="block truncate font-mono text-xs" title={image.path}>
        {image.path}
      </span>
      <span className="block font-mono text-xs text-muted-foreground tabular-nums">
        {repositoryImageSize(image)}
      </span>
    </>
  );

  if (src === null) {
    return (
      <li className="grid min-w-0 gap-2">
        <div className="grid aspect-square place-items-center rounded-sm border bg-surface text-muted-foreground">
          <ImageOffIcon aria-hidden="true" className="size-5" />
          <span className="sr-only">
            This image has no address the dashboard can read.
          </span>
        </div>
        <p className="min-w-0">{caption}</p>
      </li>
    );
  }

  return (
    <li className="min-w-0">
      <a
        href={src}
        target="_blank"
        rel="noopener noreferrer"
        className="grid gap-2 rounded-sm outline-offset-4 transition-opacity hover:opacity-85"
      >
        <span className="relative block aspect-square overflow-hidden rounded-sm border bg-surface">
          <Image
            src={src}
            alt={repositoryImageAlt(image)}
            fill
            sizes="(min-width: 1024px) 12rem, (min-width: 640px) 30vw, 50vw"
            unoptimized
            className="object-contain"
          />
        </span>
        <span className="block min-w-0">
          {caption}
          <span className="sr-only">, opens the full image in a new tab</span>
        </span>
      </a>
    </li>
  );
}
