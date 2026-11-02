import { describe, expect, it } from "vitest";

import type { Proposal, TaskPlan } from "@/lib/api/types";

import { agentLabel, proposedPlanOf, readPlan, scopeText } from "./plan";

const PLAN: TaskPlan = {
  goal: "Announce dark mode on X with an image",
  scope: { max_steps: 6, max_plan_changes: 2, deadline_days: 7, max_cost_usd: "1.00" },
  steps: [
    {
      agent: "research",
      id: "facts",
      request: { questions: ["Who asked for dark mode?"], topics: [], max_sources: 8, time_window: "" },
    },
    {
      agent: "studio",
      id: "image",
      request: { goal: "A launch card", platform: "x_post", exact_copy: ["Dark mode is here"], style_notes: "" },
    },
    {
      agent: "writing",
      id: "post",
      depends_on: ["image", "facts"],
      asset_from_step: "image",
      research_from_step: "facts",
      request: { goal: "announce", platform: "x", feature: "Dark mode", angle: null, variants: 3 },
    },
  ],
};

function proposal(over: Partial<Proposal> = {}): Pick<Proposal, "action_name" | "run_kind" | "payload"> {
  return {
    action_name: "confirm_plan",
    run_kind: "orchestrator-task",
    payload: { task_id: "k", confirmation: 1, plan: PLAN },
    ...over,
  };
}

describe("a task's plan as the dashboard shows it", () => {
  it("keeps the goal and each step in order with its agent", () => {
    const plan = readPlan(PLAN);
    expect(plan?.goal).toBe("Announce dark mode on X with an image");
    expect(plan?.steps.map((step) => [step.id, step.agentLabel])).toEqual([
      ["facts", "Research"],
      ["image", "Studio"],
      ["post", "Writing"],
    ]);
  });

  it("says what a research step is asked, leaving out what is empty", () => {
    expect(readPlan(PLAN)?.steps[0]?.asks).toEqual([
      { label: "Questions", lines: ["Who asked for dark mode?"] },
      { label: "Sources", lines: ["at most 8"] },
    ]);
  });

  it("quotes the words a studio step puts on its image, and flags that it can spend", () => {
    const studio = readPlan(PLAN)?.steps[1];
    expect(studio?.canSpend).toBe(true);
    expect(studio?.asks).toEqual([
      { label: "Goal", lines: ["A launch card"] },
      { label: "Platform", lines: ["X post"] },
      { label: "Words on the image", lines: ["“Dark mode is here”"] },
    ]);
  });

  it("names the earlier steps a writing step takes its image and findings from", () => {
    const writing = readPlan(PLAN)?.steps[2];
    expect(writing?.canSpend).toBe(false);
    expect(writing?.dependsOn).toEqual(["image", "facts"]);
    expect(writing?.asks).toEqual([
      { label: "Goal", lines: ["Announce a live feature"] },
      { label: "Platform", lines: ["X"] },
      { label: "Feature", lines: ["Dark mode"] },
      { label: "Variants", lines: ["3"] },
      { label: "Image", lines: ["the image of step “image”"] },
      { label: "Research", lines: ["the findings of step “facts”"] },
    ]);
  });

  it("reads the scope's cost from the text of a decimal", () => {
    expect(scopeText(readPlan(PLAN)?.scope ?? null)).toBe(
      "Within at most 6 steps, 2 changes of plan, 7 days to finish, $1.00 for the orchestrator’s own model calls. Each run it starts keeps its own limits.",
    );
  });

  it("keeps a step of an agent it does not know by its name, asking nothing", () => {
    const plan = readPlan({ goal: "g", steps: [{ agent: "publisher", id: "send", request: { to: "all" } }] });
    expect(plan?.steps).toEqual([
      { id: "send", agent: "publisher", agentLabel: "publisher", dependsOn: [], asks: [], canSpend: false },
    ]);
    expect(agentLabel("publisher")).toBe("publisher");
  });

  it("drops a step it cannot read, and a value of the wrong type", () => {
    const plan = readPlan({
      goal: "g",
      scope: { max_steps: "six" },
      steps: [null, { agent: "research", request: {} }, { agent: "research", id: "r", request: { questions: [1, "Why?"] } }],
    });
    expect(plan?.steps.map((step) => step.asks)).toEqual([[{ label: "Questions", lines: ["Why?"] }]]);
    expect(scopeText(plan?.scope ?? null)).toBeNull();
  });

  it("is none for what is no plan", () => {
    expect(readPlan(null)).toBeNull();
    expect(readPlan({ steps: [] })).toBeNull();
    expect(readPlan({ goal: "g", steps: "all" })).toBeNull();
  });
});

describe("the plan a task's confirmation asks about", () => {
  it("is read from the payload of its confirm_plan", () => {
    expect(proposedPlanOf(proposal())?.steps).toHaveLength(3);
  });

  it("is none for another action, another kind of run, or a payload without a plan", () => {
    expect(proposedPlanOf(proposal({ action_name: "publish_post" }))).toBeNull();
    expect(proposedPlanOf(proposal({ run_kind: "writing-run" }))).toBeNull();
    expect(proposedPlanOf(proposal({ payload: { plan: "the plan" } }))).toBeNull();
  });
});
