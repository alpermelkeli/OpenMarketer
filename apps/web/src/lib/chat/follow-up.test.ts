import { describe, expect, it } from "vitest";

import { followUpWording } from "./follow-up";

describe("how a follow-up turn is worded", () => {
  it("names the research when research has ended", () => {
    expect(followUpWording("research_finished").reviewing).toBe("The orchestrator is reviewing the research…");
    expect(followUpWording("research_finished").stillThere).toBe("The report is still on the run’s page.");
  });

  it("names the studio's image when a studio run has ended", () => {
    expect(followUpWording("studio_finished").reviewing).toBe("The orchestrator is reviewing the studio’s image…");
    expect(followUpWording("studio_finished").failed).toBe("The orchestrator could not review the studio’s image");
  });
});
