import { describe, expect, it } from "vitest";

import type { ResearchRun } from "@/lib/api/types";

import type { RunProposal, ResearchRunView } from "./events";
import { outcomeText, pageHost, progressStatus, proposalText, stageText } from "./research-progress";

function view(over: Partial<ResearchRunView> = {}): ResearchRunView {
  return {
    id: "r",
    turnId: "t",
    stages: [],
    status: "running",
    findings: null,
    reason: null,
    proposal: null,
    firstEventId: 1,
    startedAt: "2026-10-17T12:00:00Z",
    ...over,
  };
}

function stored(status: ResearchRun["status"], error: string | null = null): ResearchRun {
  return { status, error } as ResearchRun;
}

describe("a research run's state in its chat", () => {
  it("is what its events say while its record has not ended", () => {
    expect(progressStatus(view(), stored("running")).state).toBe("running");
    expect(progressStatus(view({ status: "finished", findings: 2 }), undefined)).toEqual({
      state: "finished",
      findings: 2,
      reason: null,
    });
  });

  it("is failed, with the record's reason, when the record says it failed without an event", () => {
    expect(progressStatus(view(), stored("failed", "The workflow could not be started."))).toEqual({
      state: "failed",
      findings: null,
      reason: "The workflow could not be started.",
    });
  });

  it("is finished when the record says it succeeded", () => {
    expect(progressStatus(view(), stored("succeeded")).state).toBe("finished");
  });
});

describe("a stage as its card says it", () => {
  it("counts what was planned, found and kept", () => {
    expect(stageText({ kind: "planned", round: 1, queries: ["a", "b"] })).toBe("Planned 2 queries");
    expect(stageText({ kind: "planned", round: 2, queries: ["a"] })).toBe("Planned 1 query for round 2");
    expect(stageText({ kind: "searched", query: "x pricing", results: 1, ok: true })).toBe("Searched “x pricing”: 1 result");
    expect(stageText({ kind: "searched", query: "x", results: 0, ok: false })).toBe("Searched “x”: the search did not answer");
    expect(stageText({ kind: "screened", kept: 3, dropped: 5 })).toBe("Kept 3 of 8 results as bearing on the request");
  });

  it("names a fetched page by its title, or by its host when it has none", () => {
    expect(stageText({ kind: "fetched", url: "https://example.com/p", title: "Pricing" })).toBe("Fetched Pricing");
    expect(stageText({ kind: "fetched", url: "https://example.com/p", title: " " })).toBe("Fetched example.com");
    expect(stageText({ kind: "fetch_failed", url: "https://example.com/p", reason: "It took too long." })).toBe(
      "Could not fetch a page of example.com: It took too long.",
    );
  });

  it("counts what was read and found", () => {
    expect(stageText({ kind: "read", pages: 2, ok: true })).toBe("Read 2 pages so far");
    expect(stageText({ kind: "read", pages: 2, ok: false })).toBe("Could not read a page");
    expect(stageText({ kind: "synthesized", findings: 1, round: 1 })).toBe("1 checked finding so far");
  });
});

describe("a page's host", () => {
  it("is the address as it came when it is not one", () => {
    expect(pageHost("not a url")).toBe("not a url");
    expect(pageHost("https://sub.example.com:8443/a?b")).toBe("sub.example.com:8443");
  });
});

describe("the end of a run's card", () => {
  it("says how many findings a finished run has", () => {
    expect(outcomeText({ state: "finished", findings: 3, reason: null })).toBe("Finished with 3 checked findings.");
    expect(outcomeText({ state: "finished", findings: 0, reason: null })).toBe("Finished without a checked finding.");
    expect(outcomeText({ state: "running", findings: 3, reason: null })).toBeNull();
  });
});

describe("the action the gate held for a run", () => {
  const proposal = (status: RunProposal["status"]): RunProposal => ({
    id: "p-1",
    actionName: "search_web",
    effect: "spends",
    status,
  });

  it("reads as a sentence for where it stands", () => {
    expect(proposalText(proposal("waiting"))).toBe(
      "The run asked to run search_web and waits for your decision. Nothing runs until you decide.",
    );
    expect(proposalText(proposal("approved"))).toBe("You approved search_web; the run carries it out.");
    expect(proposalText(proposal("rejected"))).toBe("You rejected search_web; it did not run.");
    expect(proposalText(proposal("expired"))).toBe(
      "Nobody decided on search_web before its deadline, or the run stopped waiting; it did not run.",
    );
  });
});
