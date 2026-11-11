import { describe, expect, it } from "vitest";

import { claimOnce } from "./once";

describe("a step taken once", () => {
  it("is taken the first time and not when React runs the same effect again", () => {
    const sent = { current: false };
    const requests: string[] = [];
    const effect = () => {
      if (claimOnce(sent)) requests.push("callback");
    };
    effect();
    effect();
    expect(requests).toEqual(["callback"]);
  });

  it("is taken again by another holder: another page", () => {
    expect(claimOnce({ current: false })).toBe(true);
    expect(claimOnce({ current: true })).toBe(false);
  });
});
