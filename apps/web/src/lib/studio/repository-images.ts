/**
 * How the project's repository images are shown: those an analysis found and those a person
 * uploaded. The word for each kind, the groups they fall in, their size, what is said of where
 * an image came from and who chose its kind, what removing one does, and the address the page
 * reads each from. The kind is the API's: its hint from the path, or a person's choice; this
 * module names it and does not guess one. An address is built from the ids the dashboard holds,
 * under its own proxy, never taken from a response; the path is text from the repository or a
 * file's name, and is only ever shown as text.
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

/** The kinds a person can choose, for a select. */
export const REPOSITORY_IMAGE_KIND_OPTIONS: readonly {
  value: RepositoryImageKind;
  label: string;
}[] = REPOSITORY_IMAGE_KINDS.map((value) => ({
  value,
  label: repositoryImageKindLabel(value),
}));

/** The short notes beside an image: that a person uploaded it, and that a person chose its kind. */
export function repositoryImageNotes(
  image: Pick<RepositoryImage, "source" | "kind_chosen">,
): string[] {
  const notes: string[] = [];
  if (image.source === "person") notes.push("Uploaded");
  if (image.kind_chosen) notes.push("Kind chosen by a person");
  return notes;
}

/** What the step that removes an image says, and its button: an upload is deleted, a found image hidden. */
export type RepositoryImageRemoval = {
  title: string;
  description: string;
  action: string;
  pending: string;
};

export function repositoryImageRemoval(
  image: Pick<RepositoryImage, "source">,
): RepositoryImageRemoval {
  if (image.source === "person") {
    return {
      title: "Delete this image?",
      description: "It is removed from the project.",
      action: "Delete the image",
      pending: "Deleting…",
    };
  }
  return {
    title: "Hide this image?",
    description:
      "The studio will not use it, and a later analysis keeps it hidden.",
    action: "Hide the image",
    pending: "Hiding…",
  };
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

/** What the image is, for someone who cannot see it: its kind, and where it is in the repository or the name it was uploaded as. */
export function repositoryImageAlt(
  image: Pick<RepositoryImage, "kind" | "path" | "source">,
): string {
  const kind = repositoryImageKindLabel(image.kind);
  return image.source === "person"
    ? `${kind} uploaded as ${image.path}`
    : `${kind} from the repository at ${image.path}`;
}
