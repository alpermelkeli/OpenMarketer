import { describe, expect, it } from "vitest";

import type { ChatEvent, ChatMessage } from "@/lib/api/types";

import { awaitedTurn, foldChatEvents, inOrder, MAX_HISTORY_PAGES, needsOlderEvents, newestEventId, parseChatEvent } from "./events";

const TURN = "11111111-1111-4111-8111-111111111111";
const LATER_TURN = "22222222-2222-4222-8222-222222222222";
const RUN = "33333333-3333-4333-8333-333333333333";

let nextId = 1;

function event(kind: string, detail: ChatEvent["detail"] = {}, over: Partial<ChatEvent> = {}): ChatEvent {
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

function research(kind: string, detail: ChatEvent["detail"] = {}, over: Partial<ChatEvent> = {}): ChatEvent {
  return event(kind, detail, { run_kind: "research-run", run_id: RUN, ...over });
}

function onlyTurn(events: ChatEvent[]) {
  const [turn] = foldChatEvents(events).turns;
  if (turn === undefined) throw new Error("expected a turn");
  return turn;
}

function onlyRun(events: ChatEvent[]) {
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
    expect(parseChatEvent(JSON.stringify(sent))).toEqual(sent);
  });

  it("is skipped when it is not one", () => {
    for (const data of ["", "not json", "[]", '{"id":"1"}', JSON.stringify({ ...event("x"), detail: [] })]) {
      expect(parseChatEvent(data)).toBeNull();
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
