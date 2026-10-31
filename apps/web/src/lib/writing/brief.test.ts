import { describe, expect, it } from "vitest";

import type { WritingBrief, WritingRun, WritingRunDetail } from "@/lib/api/types";

import { runBrief, variantsText } from "./brief";

const brief: WritingBrief = { platform: "x", goal: "angle", angle: "Less busywork", variants: 3 };
const listed = { id: "r", status: "running" } as WritingRun;
const detail = (served: WritingBrief | null) => ({ ...listed, brief: served }) as WritingRunDetail;

describe("what a writing run was asked for", () => {
  it("is not known yet for a run as a list gave it", () => {
    expect(runBrief(listed)).toEqual({ state: "loading" });
  });

  it("is the brief its own route served", () => {
    expect(runBrief(detail(brief))).toEqual({ state: "known", brief });
  });

  it("is unreadable when the API served none", () => {
    expect(runBrief(detail(null))).toEqual({ state: "unreadable" });
  });
});

describe("how many texts were asked for", () => {
  it("is said in words", () => {
    expect(variantsText(1)).toBe("One text");
    expect(variantsText(3)).toBe("3 texts to choose from");
  });
});
