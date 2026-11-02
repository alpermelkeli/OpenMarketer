import { describe, expect, it } from "vitest";

import type { ChatMessage, RunEvent } from "@/lib/api/types";

import { awaitedTurn, foldChatEvents, inOrder, MAX_HISTORY_PAGES, needsOlderEvents, newestEventId, parseRunEvent } from "./events";

const TURN = "11111111-1111-4111-8111-111111111111";
const LATER_TURN = "22222222-2222-4222-8222-222222222222";
const RUN = "33333333-3333-4333-8333-333333333333";
const STUDIO = "44444444-4444-4444-8444-444444444444";
const ASSET = "55555555-5555-4555-8555-555555555555";
const V1 = "66666666-6666-4666-8666-666666666666";
const V2 = "77777777-7777-4777-8777-777777777777";

let nextId = 1;

function event(kind: string, detail: RunEvent["detail"] = {}, over: Partial<RunEvent> = {}): RunEvent {
  const id = over.id ?? nextId++;
  return {
    id,
    run_kind: "orchestrator-chat",
    run_id: TURN,
    kind,
    detail,
    created_at: `2026-10-17T12:00:${String(id % 60).padStart(2, "0")}Z`,
    ...over,
  };
}

function research(kind: string, detail: RunEvent["detail"] = {}, over: Partial<RunEvent> = {}): RunEvent {
  return event(kind, detail, { run_kind: "research-run", run_id: RUN, ...over });
}

function onlyTurn(events: RunEvent[]) {
  const [turn] = foldChatEvents(events).turns;
  if (turn === undefined) throw new Error("expected a turn");
  return turn;
}

function studio(kind: string, detail: RunEvent["detail"] = {}, over: Partial<RunEvent> = {}): RunEvent {
  return event(kind, detail, { run_kind: "studio-run", run_id: STUDIO, ...over });
}

function onlyStudioRun(events: RunEvent[]) {
  const [run] = foldChatEvents(events).studioRuns;
  if (run === undefined) throw new Error("expected a studio run");
  return run;
}

function onlyRun(events: RunEvent[]) {
  const [run] = foldChatEvents(events).runs;
  if (run === undefined) throw new Error("expected a run");
  return run;
}

describe("a chat turn", () => {
  it("is running from its start, with the trigger it names", () => {
    const turn = onlyTurn([event("turn_started", { trigger: "research_finished" })]);
    expect(turn).toMatchObject({ id: TURN, status: "running", trigger: "research_finished", started: true });
  });

  it("answers a message when its trigger is not one it knows", () => {
    expect(onlyTurn([event("turn_started", { trigger: "other" })]).trigger).toBe("message");
  });

  it("lists each tool it called, waiting until the tool answers", () => {
    const turn = onlyTurn([
      event("turn_started", { trigger: "message" }),
      event("tool_called", { tool: "read_profile", label: "Reading the profile" }),
      event("tool_answered", { tool: "read_profile", ok: true }),
      event("tool_called", { tool: "list_runs", label: "Listing the runs" }),
    ]);
    expect(turn.steps).toEqual([
      { tool: "read_profile", label: "Reading the profile", ok: true },
      { tool: "list_runs", label: "Listing the runs", ok: null },
    ]);
  });

  it("marks a tool that answered with a failure", () => {
    const turn = onlyTurn([
      event("tool_called", { tool: "read_profile", label: "Reading" }),
      event("tool_answered", { tool: "read_profile", ok: false }),
    ]);
    expect(turn.steps[0]?.ok).toBe(false);
  });

  it("counts a repeated call of a tool still waiting for its answer once", () => {
    const turn = onlyTurn([
      event("tool_called", { tool: "read_profile", label: "Reading" }),
      event("tool_called", { tool: "read_profile", label: "Reading" }),
    ]);
    expect(turn.steps).toHaveLength(1);
  });

  it("keeps a second call of a tool after the first answered", () => {
    const turn = onlyTurn([
      event("tool_called", { tool: "read_profile", label: "Reading" }),
      event("tool_answered", { tool: "read_profile", ok: true }),
      event("tool_called", { tool: "read_profile", label: "Reading" }),
    ]);
    expect(turn.steps).toHaveLength(2);
  });

  it("names the message that answered it", () => {
    const turn = onlyTurn([event("turn_started"), event("turn_answered", { message_id: "m-1" })]);
    expect(turn).toMatchObject({ status: "answered", messageId: "m-1" });
  });

  it("keeps the reason it failed", () => {
    const turn = onlyTurn([event("turn_started"), event("turn_failed", { reason: "No answer within limits." })]);
    expect(turn).toMatchObject({ status: "failed", reason: "No answer within limits." });
  });

  it("lists the research runs it started, once each", () => {
    const started = { run_kind: "research-run", run_id: RUN };
    const turn = onlyTurn([event("run_started", started), event("run_started", started)]);
    expect(turn.researchRunIds).toEqual([RUN]);
  });

  it("ignores a run it started that is not research", () => {
    expect(onlyTurn([event("run_started", { run_kind: "analysis", run_id: RUN })]).researchRunIds).toEqual([]);
  });

  it("reads a detail of the wrong type as missing", () => {
    const turn = onlyTurn([event("tool_called", { tool: 5, label: "x" }), event("turn_answered", { message_id: 7 })]);
    expect(turn.steps).toEqual([]);
    expect(turn.messageId).toBeNull();
  });
});

describe("the running turn", () => {
  it("is the latest that has neither answered nor failed", () => {
    const activity = foldChatEvents([
      event("turn_started"),
      event("turn_answered", { message_id: "m-1" }),
      event("turn_started", {}, { run_id: LATER_TURN }),
    ]);
    expect(activity.runningTurn?.id).toBe(LATER_TURN);
  });

  it("is none once every turn has ended", () => {
    const activity = foldChatEvents([event("turn_started"), event("turn_failed", { reason: "x" })]);
    expect(activity.runningTurn).toBeNull();
  });
});

describe("a research run", () => {
  it("belongs to the turn that started it", () => {
    const run = onlyRun([event("run_started", { run_kind: "research-run", run_id: RUN }), research("planned", {})]);
    expect(run.turnId).toBe(TURN);
  });

  it("lists its stages in the order they were recorded", () => {
    const run = onlyRun([
      research("planned", { round: 1, queries: ["pricing of x", "x reviews"], categories: ["general"] }),
      research("searched", { query: "pricing of x", results: 8, ok: true }),
      research("screened", { kept: 3, dropped: 5 }),
      research("fetched", { url: "https://example.com/p", title: "Pricing" }),
      research("fetch_failed", { url: "https://example.com/q", reason: "It took too long." }),
      research("read", { pages: 1, ok: true }),
      research("synthesized", { findings: 2, round: 1 }),
    ]);
    expect(run.stages).toEqual([
      { kind: "planned", round: 1, queries: ["pricing of x", "x reviews"] },
      { kind: "searched", query: "pricing of x", results: 8, ok: true },
      { kind: "screened", kept: 3, dropped: 5 },
      { kind: "fetched", url: "https://example.com/p", title: "Pricing" },
      { kind: "fetch_failed", url: "https://example.com/q", reason: "It took too long." },
      { kind: "read", pages: 1, ok: true },
      { kind: "synthesized", findings: 2, round: 1 },
    ]);
    expect(run.status).toBe("running");
    expect(run.findings).toBe(2);
  });

  it("shows a stage recorded twice in a row once", () => {
    const fetched = { url: "https://example.com/p", title: "Pricing" };
    const run = onlyRun([research("fetched", fetched), research("fetched", fetched)]);
    expect(run.stages).toHaveLength(1);
  });

  it("finishes with the findings it reports", () => {
    const run = onlyRun([research("synthesized", { findings: 2, round: 1 }), research("finished", { findings: 3 })]);
    expect(run).toMatchObject({ status: "finished", findings: 3 });
  });

  it("keeps the reason it failed", () => {
    expect(onlyRun([research("failed", { reason: "No page could be fetched." })])).toMatchObject({
      status: "failed",
      reason: "No page could be fetched.",
    });
  });

  it("leaves out a stage it does not know or one without its subject", () => {
    expect(onlyRun([research("planned"), research("searched", {}), research("unknown_stage")]).stages).toEqual([
      { kind: "planned", round: null, queries: [] },
    ]);
  });

  it("holds the action the policy gate waited on", () => {
    const run = onlyRun([
      research("proposal_waiting", { proposal_id: "p-1", action_name: "search_web", effect: "spends" }),
    ]);
    expect(run.proposal).toEqual({
      id: "p-1",
      actionName: "search_web",
      effect: "spends",
      status: "waiting",
    });
    expect(run.stages).toEqual([]);
  });

  it("shows where the held action stands once the person decided or it expired", () => {
    const waiting = research("proposal_waiting", {
      proposal_id: "p-1",
      action_name: "search_web",
      effect: "spends",
    });
    const decided = (approved: boolean) =>
      onlyRun([waiting, research("proposal_decided", { proposal_id: "p-1", approved })]).proposal;
    expect(decided(true)).toMatchObject({ id: "p-1", status: "approved" });
    expect(decided(false)).toMatchObject({ id: "p-1", status: "rejected" });
    expect(onlyRun([waiting, research("proposal_expired", { proposal_id: "p-1" })]).proposal).toMatchObject({
      id: "p-1",
      status: "expired",
    });
  });

  it("counts a decision on another proposal as nothing", () => {
    const run = onlyRun([
      research("proposal_waiting", { proposal_id: "p-1", action_name: "search_web", effect: "spends" }),
      research("proposal_decided", { proposal_id: "p-2", approved: true }),
    ]);
    expect(run.proposal).toMatchObject({ id: "p-1", status: "waiting" });
  });

  it("takes the last action the gate waited on", () => {
    const run = onlyRun([
      research("proposal_waiting", { proposal_id: "p-1", action_name: "search_web", effect: "spends" }),
      research("proposal_decided", { proposal_id: "p-1", approved: true }),
      research("proposal_waiting", { proposal_id: "p-2", action_name: "fetch_page", effect: "spends" }),
    ]);
    expect(run.proposal).toEqual({
      id: "p-2",
      actionName: "fetch_page",
      effect: "spends",
      status: "waiting",
    });
  });
});

describe("events from the history and the stream", () => {
  it("are counted once by their id", () => {
    const called = event("tool_called", { tool: "read_profile", label: "Reading" });
    const answered = event("tool_answered", { tool: "read_profile", ok: true });
    const again = event("tool_called", { tool: "read_profile", label: "Reading" });
    expect(onlyTurn([called, answered, called, answered, again]).steps).toHaveLength(2);
  });

  it("are taken in the order of their ids whatever order they came in", () => {
    const first = event("turn_started");
    const second = event("turn_answered", { message_id: "m-1" });
    expect(inOrder([second, first]).map((e) => e.id)).toEqual([first.id, second.id]);
    expect(onlyTurn([second, first]).status).toBe("answered");
  });

  it("continue after the newest id", () => {
    expect(newestEventId([event("a", {}, { id: 4 }), event("b", {}, { id: 9 }), event("c", {}, { id: 2 })])).toBe(9);
    expect(newestEventId([])).toBeNull();
  });
});

describe("an event the stream sends", () => {
  it("is read from its data line", () => {
    const sent = event("tool_called", { tool: "read_profile", label: "Reading" });
    expect(parseRunEvent(JSON.stringify(sent))).toEqual(sent);
  });

  it("is skipped when it is not one", () => {
    for (const data of ["", "not json", "[]", '{"id":"1"}', JSON.stringify({ ...event("x"), detail: [] })]) {
      expect(parseRunEvent(data)).toBeNull();
    }
  });
});

describe("the history a screen loads", () => {
  const started = [event("turn_started", {}, { run_id: LATER_TURN })];

  it("goes further back while the oldest message's turn has not started in it", () => {
    const activity = foldChatEvents([event("turn_answered", { message_id: "m" })]);
    expect(needsOlderEvents({ activity, pagesLoaded: 1, hasOlderPage: true, oldestTurnId: TURN })).toBe(true);
  });

  it("is enough once that turn's start is in it", () => {
    const activity = foldChatEvents([event("turn_started"), ...started]);
    expect(needsOlderEvents({ activity, pagesLoaded: 1, hasOlderPage: true, oldestTurnId: TURN })).toBe(false);
  });

  it("stops at the oldest page, without a turn to cover, or at its limit", () => {
    const activity = foldChatEvents(started);
    expect(needsOlderEvents({ activity, pagesLoaded: 1, hasOlderPage: false, oldestTurnId: TURN })).toBe(false);
    expect(needsOlderEvents({ activity, pagesLoaded: 1, hasOlderPage: true, oldestTurnId: null })).toBe(false);
    expect(
      needsOlderEvents({ activity, pagesLoaded: MAX_HISTORY_PAGES, hasOlderPage: true, oldestTurnId: TURN }),
    ).toBe(false);
  });
});

describe("the turn a message just sent waits for", () => {
  const shown = (id: string) => [{ id } as ChatMessage];

  it("is the latest turn answering a message while it runs", () => {
    const activity = foldChatEvents([event("turn_started", { trigger: "message" })]);
    expect(awaitedTurn(activity, [])?.id).toBe(TURN);
  });

  it("stays until its answer is shown", () => {
    const activity = foldChatEvents([event("turn_started"), event("turn_answered", { message_id: "m-1" })]);
    expect(awaitedTurn(activity, [])?.id).toBe(TURN);
    expect(awaitedTurn(activity, shown("m-1"))).toBeNull();
  });

  it("is none before the new turn starts, or when the latest failed", () => {
    expect(awaitedTurn(foldChatEvents([]), [])).toBeNull();
    expect(awaitedTurn(foldChatEvents([event("turn_started"), event("turn_failed", { reason: "x" })]), [])).toBeNull();
  });

  it("is never a follow-up the orchestrator started", () => {
    const activity = foldChatEvents([event("turn_started", { trigger: "research_finished" })]);
    expect(awaitedTurn(activity, [])).toBeNull();
  });
});

describe("a studio run in its chat", () => {
  it("is tied to the turn that started it", () => {
    const activity = foldChatEvents([event("run_started", { run_kind: "studio-run", run_id: STUDIO })]);
    expect(activity.turns[0]?.studioRunIds).toEqual([STUDIO]);
    expect(activity.turns[0]?.researchRunIds).toEqual([]);
    expect(activity.studioRuns).toMatchObject([{ id: STUDIO, turnId: TURN, status: "running" }]);
    expect(activity.runs).toEqual([]);
  });

  it("folds each step it recorded into a stage", () => {
    const run = onlyStudioRun([
      studio("briefed", { technique: "generated" }),
      studio("produced", { operation: "generate_image", version_id: V1, technique: "generated", round: 1, ok: true }),
      studio("checked", { version_id: V1, passed: false, failures: ["copy"] }),
      studio("verified", { version_id: V1, passed: false, failed: ["legibility"] }),
      studio("technique_switched", { from_technique: "generated", to_technique: "background_overlay", round: 1 }),
      studio("edit_planned", { round: 2, steps: 3 }),
      studio("finalized", { version_id: V1, round: 2 }),
    ]);
    expect(run.stages).toEqual([
      { kind: "briefed", technique: "generated" },
      { kind: "produced", operation: "generate_image", versionId: V1, technique: "generated", round: 1, ok: true },
      { kind: "checked", versionId: V1, passed: false, failures: ["copy"] },
      { kind: "verified", versionId: V1, passed: false, failed: ["legibility"] },
      { kind: "technique_switched", from: "generated", to: "background_overlay", round: 1 },
      { kind: "edit_planned", round: 2, steps: 3 },
      { kind: "finalized", versionId: V1, round: 2 },
    ]);
  });

  it("remembers the newest image a step made and its asset, and not an operation that made none", () => {
    const run = onlyStudioRun([
      studio("produced", { operation: "generate_image", version_id: V1, asset_id: ASSET, technique: "generated", round: 1, ok: true }),
      studio("produced", { operation: "edit_image", version_id: null, asset_id: null, technique: "generated", round: 2, ok: false }),
    ]);
    expect(run).toMatchObject({ status: "running", assetId: ASSET, latestVersionId: V1 });
    expect(run.stages[1]).toMatchObject({ kind: "produced", ok: false, versionId: null });
  });

  it("holds a spend the policy gate waited on, and where it stands", () => {
    const waiting = studio("proposal_waiting", { proposal_id: "p-1", action_name: "generate_image", effect: "spends" });
    expect(onlyStudioRun([waiting]).proposal).toEqual({
      id: "p-1",
      actionName: "generate_image",
      effect: "spends",
      status: "waiting",
    });
    const decided = onlyStudioRun([waiting, studio("proposal_decided", { proposal_id: "p-1", approved: false })]);
    expect(decided.proposal).toMatchObject({ id: "p-1", status: "rejected" });
    expect(decided.stages).toEqual([]);
  });

  it("shows a repeated step once", () => {
    const step = { version_id: V1, passed: true, failures: [] };
    expect(onlyStudioRun([studio("checked", step), studio("checked", step)]).stages).toHaveLength(1);
  });

  it("ends with the asset and version its finished event names", () => {
    const run = onlyStudioRun([
      studio("produced", { operation: "generate_image", version_id: V1, technique: "generated", round: 1, ok: true }),
      studio("finished", { asset_id: ASSET, version_id: V2, needs_review: true }),
    ]);
    expect(run).toMatchObject({
      status: "finished",
      assetId: ASSET,
      finalVersionId: V2,
      latestVersionId: V1,
      needsReview: true,
    });
  });

  it("keeps the reason it failed", () => {
    expect(onlyStudioRun([studio("failed", { reason: "The cost cap is spent." })])).toMatchObject({
      status: "failed",
      reason: "The cost cap is spent.",
    });
  });

  it("skips a step whose detail cannot be read", () => {
    expect(onlyStudioRun([studio("briefed", { technique: 3 }), studio("produced", {})]).stages).toEqual([]);
  });

  it("is followed up by a turn that says so", () => {
    expect(onlyTurn([event("turn_started", { trigger: "studio_finished" })]).trigger).toBe("studio_finished");
    expect(onlyTurn([event("turn_started", { trigger: "something_else" })]).trigger).toBe("message");
  });
});

const TASK = "88888888-8888-4888-8888-888888888888";
const CHILD = "99999999-9999-4999-8999-999999999999";

function taskEvent(kind: string, detail: RunEvent["detail"] = {}, over: Partial<RunEvent> = {}): RunEvent {
  return event(kind, detail, { run_kind: "orchestrator-task", run_id: TASK, ...over });
}

function onlyTask(events: RunEvent[]) {
  const [task] = foldChatEvents(events).tasks;
  if (task === undefined) throw new Error("expected a task");
  return task;
}

describe("a task in its chat", () => {
  it("is followed from the turn that started it", () => {
    const activity = foldChatEvents([event("run_started", { run_kind: "orchestrator-task", run_id: TASK })]);
    expect(activity.turns[0]?.taskIds).toEqual([TASK]);
    expect(activity.tasks).toMatchObject([{ id: TASK, turnId: TURN, status: "running" }]);
  });

  it("keeps each node's stage, from what code wrote only", () => {
    const task = onlyTask([
      taskEvent("task_planned", { steps: 2, agents: ["studio", "writing"] }),
      taskEvent("plan_checked", { steps: 2, needs_confirmation: true, plan_changes: 0 }),
      taskEvent("plan_confirmation", { status: "confirmed" }),
      taskEvent("step_finished", { step: 1, agent: "studio", run_id: CHILD, status: "succeeded" }),
      taskEvent("step_finished", { step: 2, agent: "writing", run_id: CHILD, status: "succeeded", post_status: "rejected" }),
      taskEvent("step_dropped", { steps: [3, 4] }),
      taskEvent("task_reviewed", { decision: "finish" }),
      taskEvent("task_reported", { outcome: "completed", cost_usd: 0.0123 }),
    ]);
    expect(task.stages).toEqual([
      { kind: "planned", steps: 2, agents: ["studio", "writing"] },
      { kind: "checked", steps: 2, needsConfirmation: true, planChanges: 0 },
      { kind: "confirmation", status: "confirmed" },
      { kind: "step_finished", step: 1, agent: "studio", runId: CHILD, status: "succeeded", postStatus: null },
      { kind: "step_finished", step: 2, agent: "writing", runId: CHILD, status: "succeeded", postStatus: "rejected" },
      { kind: "step_dropped", steps: [3, 4] },
      { kind: "reviewed", decision: "finish" },
      { kind: "reported", outcome: "completed", costUsd: 0.0123 },
    ]);
  });

  it("takes a step that started no run as one without a run", () => {
    const task = onlyTask([taskEvent("step_finished", { step: 1, agent: "writing", run_id: "", status: "failed" })]);
    expect(task.stages).toEqual([
      { kind: "step_finished", step: 1, agent: "writing", runId: null, status: "failed", postStatus: null },
    ]);
  });

  it("skips a stage it cannot read rather than show it half-way", () => {
    const task = onlyTask([
      taskEvent("step_finished", { step: "one", agent: "studio" }),
      taskEvent("step_dropped", { steps: ["2", -1] }),
      taskEvent("plan_confirmation", { status: 3 }),
      taskEvent("task_reported", { outcome: "completed", cost_usd: "free" }),
    ]);
    expect(task.stages).toEqual([{ kind: "reported", outcome: "completed", costUsd: null }]);
  });

  it("holds its plan to confirm and where that stands", () => {
    const waiting = taskEvent("proposal_waiting", { proposal_id: "p-1", action_name: "confirm_plan", effect: "confirms" });
    expect(onlyTask([waiting]).proposal).toEqual({
      id: "p-1",
      actionName: "confirm_plan",
      effect: "confirms",
      status: "waiting",
    });
    const rejected = onlyTask([waiting, taskEvent("proposal_decided", { proposal_id: "p-1", approved: false })]);
    expect(rejected.proposal?.status).toBe("rejected");
  });

  it("ends as its workflow says", () => {
    expect(onlyTask([taskEvent("finished")]).status).toBe("finished");
    const failed = onlyTask([taskEvent("failed", { reason: "The task could not start." })]);
    expect(failed).toMatchObject({ status: "failed", reason: "The task could not start." });
  });

  it("is answered by a follow-up turn once it has ended", () => {
    const activity = foldChatEvents([event("turn_started", { trigger: "task_finished" })]);
    expect(activity.turns[0]?.trigger).toBe("task_finished");
  });
});
