import { describe, expect, it } from "vitest";

import { draftAwaitingReview, projectStates } from "./project-summary";

const nothing = { latest_draft_version: null, latest_approved_version: null, unfinished_run_id: null };
const labels = (summary: typeof nothing | object) => projectStates({ ...nothing, ...summary }).map((s) => s.label);

describe("where a project stands", () => {
  it("is not analysed when it has no run and no profile", () => {
    expect(labels({})).toEqual(["Not analysed yet"]);
  });

  it("has a draft awaiting review when nothing is approved", () => {
    expect(labels({ latest_draft_version: 1 })).toEqual(["Draft version 1 awaits review"]);
  });

  it("has a draft awaiting review when the draft is newer than the approved version", () => {
    expect(labels({ latest_draft_version: 3, latest_approved_version: 2 })).toEqual([
      "Draft version 3 awaits review",
      "Approved version 2",
    ]);
  });

  it("does not ask for review of a draft older than the approved version", () => {
    expect(labels({ latest_draft_version: 1, latest_approved_version: 2 })).toEqual(["Approved version 2"]);
    expect(draftAwaitingReview({ ...nothing, latest_draft_version: 1, latest_approved_version: 2 })).toBeNull();
  });

  it("says first that an analysis is in progress", () => {
    const run = "33333333-3333-4333-8333-333333333333";
    expect(labels({ unfinished_run_id: run })).toEqual(["Analysis in progress"]);
    expect(labels({ unfinished_run_id: run, latest_approved_version: 1 })).toEqual([
      "Analysis in progress",
      "Approved version 1",
    ]);
  });
});
