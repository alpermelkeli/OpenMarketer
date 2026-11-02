import { describe, expect, it } from "vitest";

import type { Proposal, TaskDetail, TaskStepRun } from "@/lib/api/types";
import { readPlan } from "@/lib/tasks/plan";

import type { RunProposal, TaskRunView } from "./events";
import {
  reportMadeText,
  reportResearchLinks,
  stepRows,
  stepRunHref,
  taskOutcomeText,
  taskProgressStatus,
  taskStageText,
  taskStatusDisplay,
  waitingPostsOf,
} from "./task-progress";

const PROJECT = "11111111-1111-4111-8111-111111111111";
const RUN = "33333333-3333-4333-8333-333333333333";
const OTHER = "44444444-4444-4444-8444-444444444444";

function view(over: Partial<TaskRunView> = {}): TaskRunView {
  return {
    id: "k",
    turnId: "t",
    stages: [],
    status: "running",
    reason: null,
    proposal: null,
    firstEventId: 1,
    startedAt: "2026-11-02T12:00:00Z",
    ...over,
  };
}

function stored(status: TaskDetail["status"], over: Partial<TaskDetail> = {}) {
  return { status, outcome: null, error: null, ...over };
}

function dispatched(stepId: string, agent: TaskStepRun["agent"], runId: string, outcome: TaskStepRun["outcome"]): TaskStepRun {
  return { step_id: stepId, agent, run_id: runId, outcome, created_at: "2026-11-02T12:00:00Z", finished_at: null };
}

const waitingPlan: RunProposal = { id: "p", actionName: "confirm_plan", effect: "confirms", status: "waiting" };

describe("a task's state in its chat", () => {
  it("takes the record's end over its events", () => {
    const failed = taskProgressStatus(view(), stored("failed", { error: "No worker took it." }));
    expect(failed).toEqual({ state: "failed", outcome: null, reason: "No worker took it." });
    const done = taskProgressStatus(view(), stored("succeeded", { outcome: "plan_rejected" }));
    expect(done).toEqual({ state: "succeeded", outcome: "plan_rejected", reason: null });
  });

  it("reads how it came out from its report's event before the record is read again", () => {
    const reported = view({ status: "finished", stages: [{ kind: "reported", outcome: "incomplete", costUsd: 0.01 }] });
    expect(taskProgressStatus(reported, stored("running"))).toEqual({
      state: "succeeded",
      outcome: "incomplete",
      reason: null,
    });
  });

  it("is queued only while the record says so and nothing has been recorded", () => {
    expect(taskProgressStatus(view(), stored("queued")).state).toBe("queued");
    expect(taskProgressStatus(view(), undefined).state).toBe("running");
  });

  it("says when its plan waits for the person", () => {
    const running = taskProgressStatus(view(), stored("running"));
    expect(taskStatusDisplay(running, waitingPlan)).toEqual({ label: "Waiting for you to confirm", tone: "warning" });
    expect(taskStatusDisplay(running, null)).toEqual({ label: "Working", tone: "progress" });
  });

  it("names how it came out once it has ended", () => {
    const status = { state: "succeeded" as const, outcome: "plan_expired", reason: null };
    expect(taskStatusDisplay(status, null).label).toBe("Plan expired");
    expect(taskOutcomeText(status)).toBe("Nobody confirmed its plan before its deadline, so nothing ran.");
    expect(taskOutcomeText({ state: "succeeded", outcome: "something_new", reason: null })).toBe(
      "It ended with its report.",
    );
    expect(taskOutcomeText({ state: "running", outcome: null, reason: null })).toBeNull();
  });
});

describe("what a task's node did, as a line of its card", () => {
  it("names the agents of the plan in order", () => {
    expect(taskStageText({ kind: "planned", steps: 2, agents: ["studio", "writing"] })).toBe(
      "Planned 2 steps: Studio, then Writing",
    );
  });

  it("says whether the checked plan waits for a confirmation", () => {
    expect(taskStageText({ kind: "checked", steps: 2, needsConfirmation: true, planChanges: 1 })).toBe(
      "Checked the plan (changed 1 time): 2 steps; it waits for you to confirm it",
    );
  });

  it("says how a step ended and what became of its post", () => {
    const finished = { kind: "step_finished" as const, step: 2, agent: "writing", runId: RUN, status: "succeeded" };
    expect(taskStageText({ ...finished, postStatus: "dry_run" })).toBe(
      "Step 2 (Writing) finished: the post was approved and recorded as a dry run",
    );
    expect(taskStageText({ ...finished, runId: null, status: "failed", postStatus: null })).toBe(
      "Step 2 (Writing) failed before its run started",
    );
  });

  it("says which steps were dropped, the report and its cost", () => {
    expect(taskStageText({ kind: "step_dropped", steps: [2, 3] })).toBe(
      "Dropped steps 2 and 3: what they waited for did not succeed",
    );
    expect(taskStageText({ kind: "reported", outcome: "completed", costUsd: 0.0123 })).toBe(
      "Wrote its report; its own model calls cost $0.0123",
    );
  });
});

describe("the page of a step's run", () => {
  it("is the run's page of its agent", () => {
    expect(stepRunHref(PROJECT, "research", RUN)).toBe(`/projects/${PROJECT}/research/${RUN}`);
    expect(stepRunHref(PROJECT, "studio", RUN)).toBe(`/projects/${PROJECT}/studio/${RUN}`);
    expect(stepRunHref(PROJECT, "writing", RUN)).toBe(`/projects/${PROJECT}/writing/${RUN}`);
  });

  it("is none for an id that is none, or an agent the dashboard does not know", () => {
    expect(stepRunHref(PROJECT, "writing", null)).toBeNull();
    expect(stepRunHref(PROJECT, "writing", "../settings")).toBeNull();
    expect(stepRunHref(PROJECT, "publisher", RUN)).toBeNull();
  });
});

describe("where each step of the plan stands", () => {
  const plan = readPlan({
    goal: "g",
    steps: [
      { agent: "studio", id: "image", request: {} },
      { agent: "writing", id: "post", request: {} },
      { agent: "research", id: "facts", request: {} },
    ],
  });

  it("is the outcome of the run it started, running until there is one, and not started otherwise", () => {
    const rows = stepRows(plan?.steps ?? [], [
      dispatched("image", "studio", RUN, "needs_review"),
      dispatched("post", "writing", OTHER, null),
    ]);
    expect(rows.map((row) => [row.place, row.state, row.runId])).toEqual([
      [1, "needs_review", RUN],
      [2, "running", OTHER],
      [3, "not_started", null],
    ]);
  });
});

describe("the posts of a task that wait for a person", () => {
  const pending = { id: "p-9", status: "pending", run_id: OTHER, action_name: "publish_post" } as Proposal;

  it("are found by the run of each writing step still going", () => {
    const posts = waitingPostsOf([pending], [dispatched("post", "writing", OTHER, null)]);
    expect([...posts]).toEqual([[OTHER, "p-9"]]);
  });

  it("leave out a step of another agent and one that has ended", () => {
    expect(waitingPostsOf([pending], [dispatched("post", "writing", OTHER, "succeeded")]).size).toBe(0);
    expect(waitingPostsOf([pending], [dispatched("image", "studio", OTHER, null)]).size).toBe(0);
  });
});

describe("a task's report", () => {
  it("counts what it made", () => {
    expect(reportMadeText({ summary: "s", content_item_ids: ["a", "b"], asset_ids: ["c"], research_run_ids: [RUN] })).toBe(
      "It made 2 posts, 1 image and 1 research report.",
    );
    expect(reportMadeText({ summary: "s" })).toBeNull();
  });

  it("links the research reports it used, only by ids that are ids", () => {
    expect(reportResearchLinks(PROJECT, { summary: "s", research_run_ids: [RUN, "javascript:alert(1)"] })).toEqual([
      { runId: RUN, href: `/projects/${PROJECT}/research/${RUN}` },
    ]);
  });
});
