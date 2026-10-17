import { describe, expect, it } from "vitest";

import { queryKeys, refreshedByChatEvent } from "./query-keys";
import type { ChatEvent } from "./types";

const PROJECT = "p";
const CHAT = "c";

function event(run_kind: string, kind: string, run_id = "r"): ChatEvent {
  return { id: 1, run_kind, run_id, kind, detail: {}, created_at: "2026-10-17T12:00:00Z" };
}

const refreshed = (e: ChatEvent) => refreshedByChatEvent(PROJECT, CHAT, e).map((r) => r.queryKey);

describe("what a chat's event refreshes", () => {
  it("is the chat alone when a turn starts, not its events", () => {
    expect(refreshedByChatEvent(PROJECT, CHAT, event("orchestrator-chat", "turn_started"))).toEqual([
      { queryKey: queryKeys.chat(PROJECT, CHAT), exact: true },
    ]);
  });

  it("is the chat, its messages and the list when a turn ends", () => {
    for (const kind of ["turn_answered", "turn_failed"]) {
      expect(refreshed(event("orchestrator-chat", kind))).toEqual([
        queryKeys.chat(PROJECT, CHAT),
        queryKeys.chatMessages(PROJECT, CHAT),
        queryKeys.chats(PROJECT),
      ]);
    }
  });

  it("is the run, the project's runs and the chat when a research run ends", () => {
    expect(refreshed(event("research-run", "finished", "run-1"))).toEqual([
      queryKeys.researchRun(PROJECT, "run-1"),
      queryKeys.researches(PROJECT),
      queryKeys.chat(PROJECT, CHAT),
    ]);
  });

  it("is nothing for a step", () => {
    expect(refreshed(event("orchestrator-chat", "tool_called"))).toEqual([]);
    expect(refreshed(event("research-run", "fetched"))).toEqual([]);
  });
});
