import { describe, expect, it } from "vitest";

import { saveFile, versionFileName, type FileSaver } from "./download";

const VERSION = "0b0e6c6e-9999-4aaa-8bbb-ccccddddeeee";

describe("the name of a downloaded version", () => {
  it("is the version's id with the extension the API gives its type", () => {
    expect(versionFileName(VERSION, "image/png")).toBe(`${VERSION}.png`);
    expect(versionFileName(VERSION, "image/jpeg")).toBe(`${VERSION}.jpg`);
    expect(versionFileName(VERSION, "image/webp")).toBe(`${VERSION}.webp`);
  });

  it("reads the type however it is written", () => {
    expect(versionFileName(VERSION, " Image/PNG ")).toBe(`${VERSION}.png`);
  });

  it("has no extension when the type is not one the API stores", () => {
    expect(versionFileName(VERSION, "text/html")).toBe(VERSION);
    expect(versionFileName(VERSION, "")).toBe(VERSION);
  });
});

function recordingSaver(clickFails = false): { saver: FileSaver; steps: string[] } {
  const steps: string[] = [];
  return {
    steps,
    saver: {
      createObjectURL: () => {
        steps.push("create");
        return "blob:local/1";
      },
      click: (href, fileName) => {
        steps.push(`click ${href} ${fileName}`);
        if (clickFails) throw new Error("blocked");
      },
      revokeObjectURL: (href) => steps.push(`revoke ${href}`),
    },
  };
}

describe("saving a file", () => {
  it("follows a link to the bytes under the file's name, then lets the URL go", () => {
    const { saver, steps } = recordingSaver();
    saveFile(new Blob(["x"], { type: "image/png" }), `${VERSION}.png`, saver);
    expect(steps).toEqual(["create", `click blob:local/1 ${VERSION}.png`, "revoke blob:local/1"]);
  });

  it("lets the URL go even when the click fails", () => {
    const { saver, steps } = recordingSaver(true);
    expect(() => saveFile(new Blob(["x"]), "f", saver)).toThrow("blocked");
    expect(steps.at(-1)).toBe("revoke blob:local/1");
  });
});
