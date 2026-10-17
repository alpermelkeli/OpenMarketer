import { describe, expect, it } from "vitest";

import { SETTINGS_SECTIONS, isSectionOpen } from "./sections";

describe("the sections of the settings", () => {
  it("end with the creative budget", () => {
    expect(SETTINGS_SECTIONS.map((section) => section.label)).toEqual([
      "Credentials",
      "Models",
      "Analysis",
      "Creative budget",
    ]);
  });

  it("are open at their own address and below it, and no other", () => {
    const budget = SETTINGS_SECTIONS[3];
    expect(isSectionOpen(budget, "/settings/creative-budget")).toBe(true);
    expect(isSectionOpen(budget, "/settings/creative-budget-extra")).toBe(false);
  });
});
