import { describe, expect, it } from "vitest";

import type { RepositoryImage, RepositoryImageKind } from "@/lib/api/types";

import {
  repositoryImageAlt,
  repositoryImageGroups,
  repositoryImageKindLabel,
  repositoryImageSize,
  repositoryImageSrc,
} from "./repository-images";

const PROJECT = "8ce5e2bb-6e54-49b4-9ddc-b96f20ec1109";
const IMAGE = "0b0e6c6e-1111-4222-8333-444455556666";

function image(
  kind: RepositoryImageKind,
  path: string,
  id = path,
): RepositoryImage {
  return {
    id,
    analysis_run_id: "0b0e6c6e-aaaa-4bbb-8ccc-ddddeeeeffff",
    path,
    kind,
    mime: "image/png",
    width: 512,
    height: 256,
    size_bytes: 1024,
    created_at: "2026-10-19T12:00:00Z",
  };
}

describe("the word for a repository image's kind", () => {
  it("is a plain word for each kind", () => {
    expect(
      ["icon", "logo", "screenshot", "marketing", "other"].map((k) =>
        repositoryImageKindLabel(k as RepositoryImageKind),
      ),
    ).toEqual(["Icon", "Logo", "Screenshot", "Marketing", "Other"]);
  });
});

describe("the groups of repository images", () => {
  it("follow the order of kinds, each by path, leaving out an empty kind", () => {
    const groups = repositoryImageGroups([
      image("other", "z.png"),
      image("screenshot", "docs/b.png"),
      image("icon", "app/icon.png"),
      image("screenshot", "docs/a.png"),
    ]);
    expect(groups.map((g) => g.label)).toEqual(["Icon", "Screenshot", "Other"]);
    expect(groups[1].images.map((i) => i.path)).toEqual([
      "docs/a.png",
      "docs/b.png",
    ]);
  });

  it("are none when there is no image", () => {
    expect(repositoryImageGroups([])).toEqual([]);
  });
});

describe("the size of a repository image", () => {
  it("is its width and height in pixels", () => {
    expect(repositoryImageSize({ width: 1280, height: 720 })).toBe(
      "1280 × 720 px",
    );
  });
});

describe("the address of a repository image", () => {
  it("is built under the dashboard's proxy from the ids", () => {
    expect(repositoryImageSrc(PROJECT, IMAGE)).toBe(
      `/api/v1/projects/${PROJECT}/repository-images/${IMAGE}/image`,
    );
  });

  it("is none when an id is not one, whatever it holds", () => {
    for (const imageId of [
      "https://evil.example/x.png",
      "../../settings",
      `${IMAGE}/../x`,
      "logo.png",
      "",
    ]) {
      expect(repositoryImageSrc(PROJECT, imageId)).toBeNull();
    }
    expect(repositoryImageSrc("javascript:alert(1)", IMAGE)).toBeNull();
  });
});

describe("the text that stands for a repository image", () => {
  it("says its kind and its path", () => {
    expect(repositoryImageAlt({ kind: "logo", path: "assets/logo.png" })).toBe(
      "Logo from the repository at assets/logo.png",
    );
  });
});
