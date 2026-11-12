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

  it("names the post when a writing run has ended", () => {
    expect(followUpWording("writing_finished").reviewing).toBe("The orchestrator is reviewing the post…");
    expect(followUpWording("writing_finished").stillThere).toBe("The post is still on the run’s page.");
  });
});

describe("how a follow-up on a task is worded", () => {
  it("names the task's report", () => {
    expect(followUpWording("task_finished").failed).toBe("The orchestrator could not report on the task");
    expect(followUpWording("task_finished").stillThere).toBe("The task’s report is still in its card in this chat.");
  });

  it("names the task's progress when one of its steps has ended", () => {
    expect(followUpWording("task_step_finished").reviewing).toBe("The orchestrator is writing up the task’s progress…");
    expect(followUpWording("task_step_finished").failed).toBe("The orchestrator could not write up the task’s progress");
  });
});
