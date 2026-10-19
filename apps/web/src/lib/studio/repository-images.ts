/**
 * How the images an analysis found in the repository are shown: the word for each kind, the
 * groups they fall in, their size, and the address the page reads each from. The kind is the
 * API's hint from the image's path; this module names it and does not guess one. An address is
 * built from the ids the dashboard holds, under its own proxy, never taken from a response; the
 * path is text from the repository and is only ever shown as text.
 */

import type { RepositoryImage, RepositoryImageKind } from "@/lib/api/types";
import { isProjectId, isRepositoryImageId } from "@/lib/projects/project-id";

/** Every kind, in the order the screen shows their groups: what names the product first. */
export const REPOSITORY_IMAGE_KINDS: readonly RepositoryImageKind[] = [
  "icon",
  "logo",
  "screenshot",
  "marketing",
  "other",
];

const KIND_LABELS: Record<RepositoryImageKind, string> = {
  icon: "Icon",
  logo: "Logo",
  screenshot: "Screenshot",
  marketing: "Marketing",
  other: "Other",
};

export function repositoryImageKindLabel(kind: RepositoryImageKind): string {
  return KIND_LABELS[kind];
}

export type RepositoryImageGroup = {
  kind: RepositoryImageKind;
  label: string;
  images: RepositoryImage[];
};

/** The images grouped by kind, in `REPOSITORY_IMAGE_KINDS` order, each group by path; empty groups are left out. */
export function repositoryImageGroups(
  images: readonly RepositoryImage[],
): RepositoryImageGroup[] {
  return REPOSITORY_IMAGE_KINDS.map((kind) => ({
    kind,
    label: repositoryImageKindLabel(kind),
    images: images
      .filter((image) => image.kind === kind)
      .sort((a, b) => a.path.localeCompare(b.path)),
  })).filter((group) => group.images.length > 0);
}

/** The image's size in pixels, as "1280 × 720 px". */
export function repositoryImageSize(
  image: Pick<RepositoryImage, "width" | "height">,
): string {
  return `${image.width} × ${image.height} px`;
}

/** The address the page shows the image from, or null when an id is not one. */
export function repositoryImageSrc(
  projectId: string,
  imageId: string,
): string | null {
  if (!isProjectId(projectId) || !isRepositoryImageId(imageId)) return null;
  const segments = [
    "projects",
    projectId,
    "repository-images",
    imageId,
    "image",
  ];
  return `/api/v1/${segments.map(encodeURIComponent).join("/")}`;
}

/** What the image is, for someone who cannot see it: its kind and where it is in the repository. */
export function repositoryImageAlt(
  image: Pick<RepositoryImage, "kind" | "path">,
): string {
  return `${repositoryImageKindLabel(image.kind)} from the repository at ${image.path}`;
}
