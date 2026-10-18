import { describe, expect, it } from "vitest";

import {
  queryKeys,
  refreshedByAssetDecision,
  refreshedByChatEvent,
  refreshedByStudioRunDeleted,
  refreshedByStudioRunEnd,
  refreshedByWorkspaceEvent,
} from "./query-keys";
import type { RunEvent } from "./types";

const PROJECT = "p";
const CHAT = "c";
const PROPOSAL_KINDS = ["proposal_waiting", "proposal_decided", "proposal_expired"];

function event(run_kind: string, kind: string, run_id = "r"): RunEvent {
  return { id: 1, run_kind, run_id, kind, detail: {}, created_at: "2026-10-17T12:00:00Z" };
}

const refreshed = (e: RunEvent) => refreshedByChatEvent(PROJECT, CHAT, e).map((r) => r.queryKey);

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

  it("is the list of the kind of run a turn started", () => {
    const started = (run_kind: string) => ({ ...event("orchestrator-chat", "run_started"), detail: { run_kind, run_id: "x" } });
    expect(refreshed(started("research-run"))).toEqual([queryKeys.researches(PROJECT)]);
    expect(refreshed(started("studio-run"))).toEqual([queryKeys.studioRuns(PROJECT)]);
    expect(refreshed(started("analysis"))).toEqual([]);
  });

  it("is the run, the studio's runs, the library, the month's spending and the chat when a studio run ends", () => {
    for (const kind of ["finished", "failed"]) {
      expect(refreshed(event("studio-run", kind, "run-1"))).toEqual([
        queryKeys.studioRun(PROJECT, "run-1"),
        queryKeys.studioRuns(PROJECT),
        queryKeys.mediaAssets(PROJECT),
        queryKeys.creativeBudget(),
        queryKeys.chat(PROJECT, CHAT),
      ]);
    }
  });

  it("is the project's proposals when the gate held an action of a run or it was released", () => {
    for (const runKind of ["research-run", "studio-run"]) {
      for (const kind of PROPOSAL_KINDS) {
        expect(refreshed(event(runKind, kind, "run-1"))).toEqual([queryKeys.proposals(PROJECT)]);
      }
    }
  });

  it("is nothing for a step", () => {
    expect(refreshed(event("orchestrator-chat", "tool_called"))).toEqual([]);
    expect(refreshed(event("research-run", "fetched"))).toEqual([]);
    expect(refreshed(event("studio-run", "produced"))).toEqual([]);
  });
});

describe("the studio's keys", () => {
  it("sit under the project, so deleting the project drops them", () => {
    const scope = queryKeys.projectScope(PROJECT);
    for (const key of [
      queryKeys.studioRuns(PROJECT),
      queryKeys.studioRun(PROJECT, "r"),
      queryKeys.mediaAssets(PROJECT),
      queryKeys.mediaAsset(PROJECT, "a"),
    ]) {
      expect(key.slice(0, scope.length)).toEqual(scope);
    }
  });

  it("keep a run out of the list's key, so polling one does not read the list again", () => {
    const list = queryKeys.studioRuns(PROJECT);
    expect(queryKeys.studioRun(PROJECT, "r").slice(0, list.length)).not.toEqual(list);
  });
});

describe("what a change in the studio refreshes", () => {
  it("is the runs, the library and the month's spending when a run ends", () => {
    expect(refreshedByStudioRunEnd(PROJECT).map((r) => r.queryKey)).toEqual([
      queryKeys.studioRuns(PROJECT),
      queryKeys.mediaAssets(PROJECT),
      queryKeys.creativeBudget(),
    ]);
  });

  it("is the library when an asset is accepted or rejected", () => {
    expect(refreshedByAssetDecision(PROJECT).map((r) => r.queryKey)).toEqual([queryKeys.mediaAssets(PROJECT)]);
  });

  it("is the runs and the library when a run is deleted", () => {
    expect(refreshedByStudioRunDeleted(PROJECT).map((r) => r.queryKey)).toEqual([
      queryKeys.studioRuns(PROJECT),
      queryKeys.mediaAssets(PROJECT),
    ]);
  });
});

describe("what a workspace event refreshes", () => {
  const refreshedAnywhere = (e: RunEvent) => refreshedByWorkspaceEvent(e).map((r) => r.queryKey);

  it("is the waiting list when a proposal is stored, decided or expires", () => {
    for (const kind of PROPOSAL_KINDS) {
      expect(refreshedAnywhere(event("research-run", kind))).toEqual([queryKeys.waitingProposals()]);
    }
  });

  it("is nothing for anything else", () => {
    expect(refreshedAnywhere(event("research-run", "searched"))).toEqual([]);
    expect(refreshedAnywhere(event("orchestrator-chat", "turn_started"))).toEqual([]);
  });
});
