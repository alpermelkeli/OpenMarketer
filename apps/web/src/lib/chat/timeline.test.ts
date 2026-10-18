import { describe, expect, it } from "vitest";

import type { ChatMessage } from "@/lib/api/types";

import type { ResearchRunView, StudioRunView, TurnView } from "./events";
import { chatTimeline, type TimelineItem } from "./timeline";

function message(id: string, minute: number, turnId: string | null = null): ChatMessage {
  return {
    id,
    chat_id: "c",
    project_id: "p",
    role: "assistant",
    content: id,
    turn_id: turnId,
    created_at: `2026-10-17T12:${String(minute).padStart(2, "0")}:00Z`,
  };
}

function turn(id: string, over: Partial<TurnView> = {}): TurnView {
  return {
    id,
    trigger: "message",
    status: "answered",
    steps: [],
    messageId: null,
    reason: null,
    researchRunIds: [],
    studioRunIds: [],
    started: true,
    firstEventId: 1,
    startedAt: "2026-10-17T12:00:00Z",
    ...over,
  };
}

function run(id: string, turnId: string | null, minute: number, firstEventId: number): ResearchRunView {
  return {
    id,
    turnId,
    stages: [],
    status: "running",
    findings: null,
    reason: null,
    proposal: null,
    firstEventId,
    startedAt: `2026-10-17T12:${String(minute).padStart(2, "0")}:30Z`,
  };
}

function studioRun(id: string, turnId: string | null, minute: number, firstEventId: number): StudioRunView {
  return {
    id,
    turnId,
    stages: [],
    status: "running",
    assetId: null,
    finalVersionId: null,
    latestVersionId: null,
    needsReview: null,
    reason: null,
    proposal: null,
    firstEventId,
    startedAt: `2026-10-17T12:${String(minute).padStart(2, "0")}:30Z`,
  };
}

function order(items: TimelineItem[]): string[] {
  return items.map((item) => {
    if (item.kind === "message") return item.message.id;
    if (item.kind === "research") return `run:${item.run.id}`;
    if (item.kind === "studio") return `studio:${item.run.id}`;
    return `failed:${item.turn.id}`;
  });
}

describe("a chat's timeline", () => {
  it("is its messages in order when no turn started research", () => {
    expect(order(chatTimeline([message("a", 1), message("b", 2)], [], []))).toEqual(["a", "b"]);
  });

  it("puts a run right after the answer of the turn that started it", () => {
    const messages = [message("q", 1), message("answer", 2, "t"), message("later", 5)];
    const turns = [turn("t", { messageId: "answer" })];
    expect(order(chatTimeline(messages, [run("r", "t", 9, 10)], turns))).toEqual(["q", "answer", "run:r", "later"]);
  });

  it("puts a follow-up's answer after the run, as it was stored after it", () => {
    const messages = [message("answer", 2, "t"), message("follow-up", 8, "f")];
    const turns = [turn("t", { messageId: "answer" }), turn("f", { messageId: "follow-up", trigger: "research_finished" })];
    expect(order(chatTimeline(messages, [run("r", "t", 3, 10)], turns))).toEqual(["answer", "run:r", "follow-up"]);
  });

  it("keeps runs started by one turn in the order they began", () => {
    const turns = [turn("t", { messageId: "answer" })];
    const runs = [run("second", "t", 2, 20), run("first", "t", 2, 10)];
    expect(order(chatTimeline([message("answer", 2, "t")], runs, turns))).toEqual(["answer", "run:first", "run:second"]);
  });

  it("places a run whose answer is not shown by the time it began", () => {
    const messages = [message("a", 1), message("b", 5)];
    expect(order(chatTimeline(messages, [run("r", null, 3, 10)], []))).toEqual(["a", "run:r", "b"]);
  });

  it("places a run after every message when it began after them", () => {
    expect(order(chatTimeline([message("a", 1)], [run("r", null, 3, 10)], []))).toEqual(["a", "run:r"]);
  });

  it("leaves out the runs of the turn still running, which the screen shows with it", () => {
    const turns = [turn("t", { status: "running" })];
    expect(order(chatTimeline([message("a", 1)], [run("r", "t", 3, 10)], turns))).toEqual(["a"]);
  });

  it("places a follow-up that failed where it began", () => {
    const messages = [message("answer", 2, "t"), message("later", 9)];
    const turns = [
      turn("t", { messageId: "answer" }),
      turn("f", { trigger: "research_finished", status: "failed", firstEventId: 30, startedAt: "2026-10-17T12:06:00Z" }),
    ];
    expect(order(chatTimeline(messages, [], turns))).toEqual(["answer", "failed:f", "later"]);
  });

  it("does not show a failed turn that answered a message", () => {
    const turns = [turn("t", { status: "failed" })];
    expect(order(chatTimeline([message("a", 1)], [], turns))).toEqual(["a"]);
  });

  it("puts a studio run right after the answer of the turn that started it", () => {
    const messages = [message("ask", 1), message("answer", 2, "t"), message("later", 9)];
    const turns = [turn("t", { messageId: "answer" })];
    expect(order(chatTimeline(messages, [], turns, [studioRun("s", "t", 1, 5)]))).toEqual([
      "ask",
      "answer",
      "studio:s",
      "later",
    ]);
  });

  it("keeps a turn's research and studio runs in the order they began", () => {
    const messages = [message("answer", 2, "t")];
    const turns = [turn("t", { messageId: "answer" })];
    const timeline = chatTimeline(messages, [run("r", "t", 1, 8)], turns, [studioRun("s", "t", 1, 5)]);
    expect(order(timeline)).toEqual(["answer", "studio:s", "run:r"]);
  });

  it("places a studio run whose answer is not shown by the time it began", () => {
    const messages = [message("a", 1), message("b", 5)];
    expect(order(chatTimeline(messages, [], [], [studioRun("s", null, 3, 10)]))).toEqual(["a", "studio:s", "b"]);
  });

  it("leaves out the studio runs of the turn still running", () => {
    const turns = [turn("t", { status: "running" })];
    expect(order(chatTimeline([message("a", 1)], [], turns, [studioRun("s", "t", 3, 10)]))).toEqual(["a"]);
  });

  it("places a failed follow-up on a studio run where it began", () => {
    const messages = [message("answer", 2, "t"), message("later", 9)];
    const turns = [
      turn("t", { messageId: "answer" }),
      turn("f", { trigger: "studio_finished", status: "failed", firstEventId: 30, startedAt: "2026-10-17T12:06:00Z" }),
    ];
    expect(order(chatTimeline(messages, [], turns))).toEqual(["answer", "failed:f", "later"]);
  });
});
