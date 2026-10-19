import { describe, expect, it } from "vitest";

import type { StudioRun, StudioRunDetail, StudioRunRequest } from "@/lib/api/types";

import { briefGoal, copyLines, referencesText, runBrief, styleNotesText } from "./brief";

function request(over: Partial<StudioRunRequest> = {}): StudioRunRequest {
  return {
    goal: "Announce the 2.0 release",
    exact_copy: ["Version 2.0", "Out now"],
    platform: "x_post",
    style_notes: "",
    reference_asset_ids: [],
    ...over,
  };
}

const listed = { id: "r", status: "running" } as StudioRun;
const detail = (req: StudioRunRequest | null) => ({ ...listed, request: req }) as StudioRunDetail;

describe("what a studio run was asked for", () => {
  it("is not known yet for a run as a list gave it", () => {
    expect(runBrief(listed)).toEqual({ state: "loading" });
    expect(briefGoal(runBrief(listed))).toBeUndefined();
  });

  it("is the request its own route served", () => {
    const brief = runBrief(detail(request()));
    expect(brief).toEqual({ state: "known", request: request() });
    expect(briefGoal(brief)).toBe("Announce the 2.0 release");
  });

  it("is unreadable when the API served none", () => {
    expect(runBrief(detail(null))).toEqual({ state: "unreadable" });
  });
});

describe("how a request reads", () => {
  it("leaves out blank lines of copy", () => {
    expect(copyLines(request({ exact_copy: ["Out now", "  "] }))).toEqual(["Out now"]);
  });

  it("says when there are no notes on style", () => {
    expect(styleNotesText(request({ style_notes: "  " }))).toBe("No notes on style.");
    expect(styleNotesText(request({ style_notes: "Dark, quiet" }))).toBe("Dark, quiet");
  });

  it("counts the images of the library it was asked to look at", () => {
    expect(referencesText(request())).toBe("No image of the library.");
    expect(referencesText(request({ reference_asset_ids: ["a"] }))).toBe("One image of the library.");
    expect(referencesText(request({ reference_asset_ids: ["a", "b"] }))).toBe("2 images of the library.");
  });
});
