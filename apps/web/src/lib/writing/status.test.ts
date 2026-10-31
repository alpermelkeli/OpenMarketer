import { describe, expect, it } from "vitest";

import type { Proposal } from "@/lib/api/types";

import {
  GOAL_OPTIONS,
  goalLabel,
  isWritingRunFinished,
  isWritingRunWaitingForWorker,
  postStatusDisplay,
  waitingPostProposal,
  writingPlatformLabel,
  writingRunStatusDisplay,
} from "./status";

const CREATED = "2026-10-27T10:00:00Z";

describe("a writing run's state", () => {
  it("says a running run that stored its post waits for the reader", () => {
    expect(writingRunStatusDisplay({ status: "running", content_item_id: "i" })).toEqual({
      label: "Waiting for your decision",
      tone: "warning",
    });
    expect(writingRunStatusDisplay({ status: "running", content_item_id: null }).label).toBe("Writing");
  });

  it("is finished once it succeeded or failed, and only then", () => {
    expect(isWritingRunFinished({ status: "succeeded" })).toBe(true);
    expect(isWritingRunFinished({ status: "failed" })).toBe(true);
    expect(isWritingRunFinished({ status: "running" })).toBe(false);
    expect(isWritingRunFinished({ status: "queued" })).toBe(false);
  });

  it("is likely waiting for a worker after a while in the queue", () => {
    const run = { status: "queued" as const, created_at: CREATED, finished_at: null };
    expect(isWritingRunWaitingForWorker(run, Date.parse(CREATED) + 1_000)).toBe(false);
    expect(isWritingRunWaitingForWorker(run, Date.parse(CREATED) + 60_000)).toBe(true);
  });
});

describe("a post's state", () => {
  it("draws a waiting post as a draft, in the colour of what needs attention", () => {
    expect(postStatusDisplay("proposed")).toMatchObject({ tone: "warning", draft: true });
  });

  it("says an approved post was a dry run and sent nowhere", () => {
    const display = postStatusDisplay("dry_run");
    expect(display).toMatchObject({ tone: "success", draft: false });
    expect(display.meaning).toMatch(/sends nothing/);
  });

  it("says a rejected, expired or failed post was not sent", () => {
    for (const status of ["rejected", "expired", "failed"] as const) {
      expect(postStatusDisplay(status).meaning).toMatch(/not sent/);
    }
  });
});

describe("what a brief asked for", () => {
  it("offers the three goals in words", () => {
    expect(GOAL_OPTIONS.map((option) => option.value)).toEqual(["introduce", "announce", "angle"]);
    expect(goalLabel("announce")).toBe("Announce a live feature");
  });

  it("shows a goal or a platform it does not know by its name", () => {
    expect(goalLabel("thread")).toBe("thread");
    expect(writingPlatformLabel("x")).toBe("X");
    expect(writingPlatformLabel("linkedin")).toBe("linkedin");
  });
});

describe("the proposal a post waits on", () => {
  function proposal(id: string, changes: Partial<Proposal>): Proposal {
    return {
      id,
      project_id: "p",
      action_name: "publish_post",
      effect: "publishes",
      payload: {},
      estimate_usd: null,
      status: "pending",
      run_kind: "writing-run",
      run_id: "r",
      created_at: CREATED,
      expires_at: "2026-10-30T10:00:00Z",
      decided_by: null,
      decided_at: null,
      ...changes,
    };
  }

  it("is the run's pending publish_post", () => {
    const waiting = [
      proposal("a", { run_id: "other" }),
      proposal("b", { action_name: "generate_image" }),
      proposal("c", {}),
    ];
    expect(waitingPostProposal(waiting, "r")?.id).toBe("c");
  });

  it("is none when nothing of the run waits", () => {
    expect(waitingPostProposal([proposal("a", { status: "approved" })], "r")).toBeNull();
    expect(waitingPostProposal([], "r")).toBeNull();
  });
});
