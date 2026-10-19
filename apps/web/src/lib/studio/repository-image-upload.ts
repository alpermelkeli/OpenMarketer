/**
 * Adding an image to the project from this browser: what the dashboard refuses before sending,
 * how a file becomes the base64 the API takes, and what a failed upload is called. The checks here
 * only spare a person a pointless upload of a file the API would refuse anyway; the API decides
 * what it stores (it reads the bytes, not the type the browser names), and its refusal is shown
 * as it wrote it.
 */

import type { ApiError } from "@/lib/api/api-error";
import { errorMessage } from "@/lib/api/error-message";
import type { RepositoryImageKind, RepositoryImageUpload } from "@/lib/api/types";

/** The types the studio stores, as a file input names them. */
export const UPLOAD_IMAGE_TYPES: readonly string[] = [
  "image/png",
  "image/jpeg",
  "image/webp",
];

/** The largest image the API takes: 8 MiB. */
export const MAX_UPLOAD_IMAGE_BYTES = 8 * 1024 * 1024;

/** How many images a person may upload to one project, as the API counts them. */
export const MAX_UPLOADED_IMAGES = 24;

/** The kind an added image has until a person picks another. */
export const DEFAULT_UPLOAD_KIND: RepositoryImageKind = "other";

type FileFacts = { type: string; size: number };

/** Why the dashboard will not send this file, in words for the person; null when it may be sent. */
export function uploadRefusal(file: FileFacts): string | null {
  if (!UPLOAD_IMAGE_TYPES.includes(file.type)) {
    return "This file is not a PNG, JPEG or WebP image, which are the only kinds the studio stores.";
  }
  if (file.size === 0) return "This file is empty.";
  if (file.size > MAX_UPLOAD_IMAGE_BYTES) {
    return `This image is ${formatMiB(file.size)}, larger than the 8 MiB the studio takes.`;
  }
  return null;
}

/** A size in mebibytes with one decimal, rounded up so that a size over a limit never reads as the limit: "9.5 MiB". */
export function formatMiB(bytes: number): string {
  return `${(Math.ceil((bytes / (1024 * 1024)) * 10) / 10).toFixed(1)} MiB`;
}

// What `FileReader.readAsDataURL` writes: a media type, `;base64`, and the bytes.
const DATA_URL = /^data:[^,;]*(;[^,;]*)*;base64,([A-Za-z0-9+/]*={0,2})$/;

/** The base64 of a data URL, without its `data:…;base64,` prefix; throws when it is not one. */
export function base64OfDataUrl(dataUrl: string): string {
  const match = DATA_URL.exec(dataUrl);
  if (match === null) throw new Error("the file could not be read as base64");
  return match[2];
}

/** A file read by the browser as a data URL. */
export function readAsDataUrl(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () =>
      typeof reader.result === "string"
        ? resolve(reader.result)
        : reject(new Error("the file could not be read"));
    reader.onerror = () => reject(new Error("the file could not be read"));
    reader.readAsDataURL(file);
  });
}

/** A file's bytes in standard base64, as the API takes them; `read` is the browser's reader unless a test stands in for it. */
export async function readFileAsBase64(
  file: Blob,
  read: (file: Blob) => Promise<string> = readAsDataUrl,
): Promise<string> {
  return base64OfDataUrl(await read(file));
}

/** What the API is sent: the file's name, the kind chosen for it and its bytes. */
export function uploadOf(
  name: string,
  kind: RepositoryImageKind,
  dataBase64: string,
): RepositoryImageUpload {
  return { name, kind, data_base64: dataBase64 };
}

/** What to tell a person about an upload that failed. The API's own refusal is safe to show, as text. */
export function uploadProblem(error: ApiError): string {
  switch (error.code) {
    case "repository_image_refused":
      return error.message;
    case "repository_image_too_large":
    case "proxy_body_rejected":
      return "The image is larger than the studio takes (8 MiB).";
    case "too_many_repository_images":
      return `The project already holds ${MAX_UPLOADED_IMAGES} uploaded images. Remove one to add another.`;
    case "storage_unavailable":
      return "The API cannot reach its object storage, so the image was not stored. Start the dev stack with `make up`, then try again.";
    case "request_invalid":
      return "The API did not accept the file's name or kind.";
    default:
      return errorMessage(error);
  }
}
