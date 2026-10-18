/**
 * Where the page reads a version's image: an address the dashboard builds from the ids it
 * holds, under its own proxy, never one taken from a response. Each id is checked to be a
 * UUID and encoded as a segment, so nothing a run wrote can choose where the browser goes.
 */

import { isMediaAssetId, isProjectId } from "@/lib/projects/project-id";

import { platformLabel } from "./status";

type VersionOf = { projectId: string; assetId: string; versionId: string };

/** The address the page shows the image from, or null when an id is not one. */
export function versionImageSrc(version: VersionOf): string | null {
  return versionPath(version, "image");
}

/** The address that saves the image as a file, or null when an id is not one. */
export function versionDownloadHref(version: VersionOf): string | null {
  return versionPath(version, "download");
}

function versionPath({ projectId, assetId, versionId }: VersionOf, last: "image" | "download"): string | null {
  if (!isProjectId(projectId) || !isMediaAssetId(assetId) || !isMediaAssetId(versionId)) return null;
  const segments = ["projects", projectId, "media-assets", assetId, "versions", versionId, last];
  return `/api/v1/${segments.map(encodeURIComponent).join("/")}`;
}

type AltDetail = {
  round?: number;
  draft?: boolean;
  /** What the run was asked for, where the screen has it: the asset alone does not serve it. */
  goal?: string;
};

/**
 * What an image is, for someone who cannot see it: what it is for, which candidate it is, and,
 * where the screen has the run's request, the goal it was made to meet. The goal is what a
 * person or a model wrote, and an alt text is text.
 */
export function assetImageAlt(platform: string, detail: AltDetail = {}): string {
  const kind = detail.draft ? "Draft image" : "Image";
  const round = detail.round === undefined ? "" : `, a candidate of round ${detail.round}`;
  const goal = detail.goal?.trim() ?? "";
  const asked = goal === "" ? "" : `. Asked for: ${goal}`;
  return `${kind} made for ${articleFor(platformLabel(platform))}${round}${asked}`;
}

// "an X post": the letter is said "ex".
function articleFor(noun: string): string {
  return /^([aeiou]|x\s)/i.test(noun) ? `an ${noun}` : `a ${noun}`;
}
