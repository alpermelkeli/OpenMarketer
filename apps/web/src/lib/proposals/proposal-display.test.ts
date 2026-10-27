import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api/api-error";
import type { Proposal } from "@/lib/api/types";

import {
  actionLabel,
  decideProblem,
  effectLabel,
  effectNote,
  estimateText,
  payloadText,
  proposalStatusDisplay,
  proposedPostText,
  splitByDecision,
  timeLeftToDecide,
} from "./proposal-display";

const NOW = Date.parse("2026-10-14T12:00:00Z");

function proposal(id: string, status: Proposal["status"]): Proposal {
  return {
    id,
    project_id: "p",
    action_name: "publish_post",
    effect: "publishes",
    payload: { text: "v2 is out" },
    estimate_usd: null,
    status,
    run_kind: "writer-run",
    run_id: "r",
    created_at: "2026-10-14T11:00:00Z",
    expires_at: "2026-10-17T11:00:00Z",
    decided_by: null,
    decided_at: null,
  };
}

describe("a proposal's state", () => {
  it("says a pending proposal waits for the reader, in the colour of what needs attention", () => {
    expect(proposalStatusDisplay("pending")).toEqual({ label: "Waiting for you", tone: "warning" });
  });

  it("has a word for every state", () => {
    expect(["approved", "rejected", "expired"].map((s) => proposalStatusDisplay(s as Proposal["status"]).label)).toEqual([
      "Approved",
      "Rejected",
      "Expired",
    ]);
  });
});

describe("what an action does", () => {
  it("is said in plain words", () => {
    expect(effectLabel("publishes")).toBe("Publishes");
    expect(effectLabel("spends")).toBe("Spends money");
  });

  it("is said as a sentence before an approval", () => {
    expect(effectNote("publishes")).toBe("It puts something in front of other people.");
    expect(effectNote("spends")).toBe("It costs money.");
  });
});

describe("the list of proposals", () => {
  it("is split into those that wait and those that are decided, keeping the order", () => {
    const split = splitByDecision([
      proposal("a", "pending"),
      proposal("b", "approved"),
      proposal("c", "pending"),
      proposal("d", "expired"),
    ]);
    expect(split.waiting.map((p) => p.id)).toEqual(["a", "c"]);
    expect(split.decided.map((p) => p.id)).toEqual(["b", "d"]);
  });
});

describe("what a proposal asks", () => {
  it("is indented JSON text", () => {
    expect(payloadText({ text: "<b>hi</b>", tags: ["a"] })).toBe('{\n  "text": "<b>hi</b>",\n  "tags": [\n    "a"\n  ]\n}');
  });
});

describe("the time left to decide", () => {
  it("is counted in the largest whole unit", () => {
    expect(timeLeftToDecide("2026-10-17T11:00:00Z", NOW)).toBe("within 2 days");
    expect(timeLeftToDecide("2026-10-14T13:30:00Z", NOW)).toBe("within 1 hour");
    expect(timeLeftToDecide("2026-10-14T12:05:30Z", NOW)).toBe("within 5 minutes");
    expect(timeLeftToDecide("2026-10-14T12:00:20Z", NOW)).toBe("within a minute");
  });

  it("says when the deadline has passed", () => {
    expect(timeLeftToDecide("2026-10-14T11:59:00Z", NOW)).toBe("after its deadline");
  });
});

describe("a refused decision", () => {
  it("says the proposal was decided already or expired", () => {
    expect(decideProblem(new ApiError(409, "proposal_not_pending", "x"))).toMatch(/already decided/);
  });

  it("says the proposal is gone", () => {
    expect(decideProblem(new ApiError(404, "proposal_not_found", "x"))).toMatch(/no longer has/);
  });
});

describe("what an action that spends shows", () => {
  it("is its estimate, when it has one", () => {
    expect(estimateText(0.12)).toBe("Estimated cost $0.12");
    expect(estimateText(0.0375)).toBe("Estimated cost $0.0375");
    expect(estimateText(null)).toBeNull();
  });

  it("names the studio's and the writer's actions in words, and no other", () => {
    expect(actionLabel("generate_image")).toBe("Generate an image");
    expect(actionLabel("edit_image")).toBe("Edit an image");
    expect(actionLabel("publish_post")).toBe("Publish a post");
    expect(actionLabel("store_draft")).toBeNull();
    expect(actionLabel("constructor")).toBeNull();
  });
});

describe("the post a writing run asks to publish", () => {
  const writing = { ...proposal("a", "pending"), run_kind: "writing-run" };

  it("is the text of its payload, as written", () => {
    expect(proposedPostText({ ...writing, payload: { content_item_id: "i", text: "<b>v2</b> is out" } })).toBe(
      "<b>v2</b> is out",
    );
  });

  it("is none for a payload without a text", () => {
    expect(proposedPostText({ ...writing, payload: { text: 3 } })).toBeNull();
    expect(proposedPostText({ ...writing, payload: { text: "" } })).toBeNull();
    expect(proposedPostText({ ...writing, payload: {} })).toBeNull();
  });

  it("is none for another action or another kind of run", () => {
    expect(proposedPostText({ ...writing, action_name: "generate_image" })).toBeNull();
    expect(proposedPostText({ ...writing, run_kind: "studio-run" })).toBeNull();
  });
});
