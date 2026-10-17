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

/**
 * What an image is, for someone who cannot see it. The request's goal is not served with the
 * asset, so it says what the image is for and which candidate it is, and no more.
 */
export function assetImageAlt(platform: string, detail: { round?: number; draft?: boolean } = {}): string {
  const kind = detail.draft ? "Draft image" : "Image";
  const round = detail.round === undefined ? "" : `, a candidate of round ${detail.round}`;
  return `${kind} made for ${articleFor(platformLabel(platform))}${round}`;
}

// "an X post": the letter is said "ex".
function articleFor(noun: string): string {
  return /^([aeiou]|x\s)/i.test(noun) ? `an ${noun}` : `a ${noun}`;
}
