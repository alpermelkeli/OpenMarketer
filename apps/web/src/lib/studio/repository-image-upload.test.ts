import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api/api-error";

import {
  MAX_UPLOAD_IMAGE_BYTES,
  base64OfDataUrl,
  readFileAsBase64,
  uploadOf,
  uploadProblem,
  uploadRefusal,
} from "./repository-image-upload";

describe("a file the dashboard will not send", () => {
  it("is anything but a PNG, a JPEG or a WebP image", () => {
    for (const type of ["image/gif", "image/svg+xml", "application/pdf", ""]) {
      expect(uploadRefusal({ type, size: 10 })).toMatch(/not a PNG, JPEG or WebP/);
    }
  });

  it("is an empty file", () => {
    expect(uploadRefusal({ type: "image/png", size: 0 })).toBe("This file is empty.");
  });

  it("is larger than 8 MiB, and says how large it is", () => {
    expect(uploadRefusal({ type: "image/jpeg", size: MAX_UPLOAD_IMAGE_BYTES + 1 })).toBe(
      "This image is 8.1 MiB, larger than the 8 MiB the studio takes.",
    );
    expect(uploadRefusal({ type: "image/jpeg", size: 10 * 1024 * 1024 })).toMatch(/10\.0 MiB/);
  });

  it("is none for an image of each type up to 8 MiB", () => {
    for (const type of ["image/png", "image/jpeg", "image/webp"]) {
      expect(uploadRefusal({ type, size: MAX_UPLOAD_IMAGE_BYTES })).toBeNull();
    }
  });
});

describe("the base64 of a file read in the browser", () => {
  it("is the data URL without its prefix", () => {
    expect(base64OfDataUrl("data:image/png;base64,iVBORw0KGgo=")).toBe("iVBORw0KGgo=");
  });

  it("keeps a prefix's parameters out of it", () => {
    expect(base64OfDataUrl("data:image/webp;name=a.webp;base64,UklGRg==")).toBe("UklGRg==");
  });

  it("is refused when the data URL is not base64", () => {
    for (const dataUrl of [
      "data:image/png,%89PNG",
      "iVBORw0KGgo=",
      "data:image/png;base64,not base64!",
      "https://example.com/a.png",
    ]) {
      expect(() => base64OfDataUrl(dataUrl)).toThrow();
    }
  });

  it("is what the browser read, stripped of its prefix", async () => {
    const read = async () => "data:image/png;base64,eA==";
    await expect(readFileAsBase64(new Blob(["x"]), read)).resolves.toBe("eA==");
  });

  it("is a failure when the browser cannot read the file, or reads what is not base64", async () => {
    const fails = async () => {
      throw new Error("the file could not be read");
    };
    await expect(readFileAsBase64(new Blob(["x"]), fails)).rejects.toThrow();
    await expect(readFileAsBase64(new Blob(["x"]), async () => "data:image/png,x")).rejects.toThrow();
  });
});

describe("what an upload sends", () => {
  it("is the file's name, the chosen kind and the bytes", () => {
    expect(uploadOf("logo.png", "logo", "eA==")).toEqual({
      name: "logo.png",
      kind: "logo",
      data_base64: "eA==",
    });
  });
});

describe("what a failed upload says", () => {
  it("is the API's own words when it refuses the image", () => {
    const error = new ApiError(400, "repository_image_refused", "The file is not an image the studio can read.");
    expect(uploadProblem(error)).toBe("The file is not an image the studio can read.");
  });

  it("names the limit when the image is too large, for the API or the proxy", () => {
    for (const code of ["repository_image_too_large", "proxy_body_rejected"] as const) {
      expect(uploadProblem(new ApiError(413, code, "too large"))).toMatch(/8 MiB/);
    }
  });

  it("says how many uploads a project holds, and what to do", () => {
    expect(uploadProblem(new ApiError(409, "too_many_repository_images", "x"))).toBe(
      "The project already holds 24 uploaded images. Remove one to add another.",
    );
  });

  it("says that storage is down", () => {
    expect(uploadProblem(new ApiError(503, "storage_unavailable", "x"))).toMatch(/object storage/);
  });
});
