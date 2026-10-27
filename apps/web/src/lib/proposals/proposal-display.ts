/**
 * How a proposal is shown: its state in words and a tone, what its action does, what it asks,
 * and how long a person has. Display only: whether an action may run is the server's decision.
 */

import type { Tone } from "@/lib/analysis/run-status";
import type { ApiError } from "@/lib/api/api-error";
import { errorMessage } from "@/lib/api/error-message";
import type { ActionEffect, Proposal, ProposalStatus } from "@/lib/api/types";

type StatusDisplay = { label: string; tone: Tone };

const STATUS_DISPLAY: Record<ProposalStatus, StatusDisplay> = {
  pending: { label: "Waiting for you", tone: "warning" },
  approved: { label: "Approved", tone: "success" },
  rejected: { label: "Rejected", tone: "danger" },
  expired: { label: "Expired", tone: "neutral" },
};

export function proposalStatusDisplay(status: ProposalStatus): StatusDisplay {
  return STATUS_DISPLAY[status];
}

const EFFECT_LABEL: Record<ActionEffect, string> = {
  publishes: "Publishes",
  spends: "Spends money",
  internal: "Internal",
};

export function effectLabel(effect: ActionEffect): string {
  return EFFECT_LABEL[effect];
}

const EFFECT_NOTE: Record<ActionEffect, string> = {
  publishes: "It puts something in front of other people.",
  spends: "It costs money.",
  internal: "It works for the run itself.",
};

/** What approving an action with `effect` lets happen, as a sentence. */
export function effectNote(effect: ActionEffect): string {
  return EFFECT_NOTE[effect];
}

// A map, so that an action another agent declares later is shown by its name alone.
const ACTION_LABELS = new Map<string, string>([
  ["generate_image", "Generate an image"],
  ["edit_image", "Edit an image"],
  ["publish_post", "Publish a post"],
]);

/** The run kind of a writing run, whose `publish_post` carries the post it would publish. */
const WRITING_RUN = "writing-run";

/**
 * The text of the post a writing run's `publish_post` asks to publish, read from its payload: the
 * words a person approves are in the proposal itself. Null for any other proposal, and for a
 * payload without a text. Written by a model: shown as text only.
 */
export function proposedPostText(proposal: Pick<Proposal, "action_name" | "run_kind" | "payload">): string | null {
  if (proposal.action_name !== "publish_post" || proposal.run_kind !== WRITING_RUN) return null;
  const text = proposal.payload.text;
  return typeof text === "string" && text !== "" ? text : null;
}

/** A short name for an action the dashboard knows, or null to show the action's own name only. */
export function actionLabel(actionName: string): string | null {
  return ACTION_LABELS.get(actionName) ?? null;
}

const ESTIMATE = new Intl.NumberFormat("en", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

/** "Estimated cost $0.12" for an action that spends and was estimated; null for any other. */
export function estimateText(estimateUsd: number | null): string | null {
  return estimateUsd === null ? null : `Estimated cost ${ESTIMATE.format(estimateUsd)}`;
}

/** The proposals that wait for a decision, and those that have one, each newest first. */
export function splitByDecision(proposals: readonly Proposal[]): { waiting: Proposal[]; decided: Proposal[] } {
  return {
    waiting: proposals.filter((proposal) => proposal.status === "pending"),
    decided: proposals.filter((proposal) => proposal.status !== "pending"),
  };
}

/** What the agent asked the action to do, as indented JSON text. It is untrusted: shown as text only. */
export function payloadText(payload: Proposal["payload"]): string {
  return JSON.stringify(payload, null, 2);
}

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/**
 * How long a person has left, to finish "Decide …": "within 3 days", "within 5 hours",
 * "within 12 minutes", "within a minute"; "after its deadline" once it has passed.
 * The server decides the deadline; the browser's clock may differ from it a little.
 */
export function timeLeftToDecide(expiresAt: string, now: number): string {
  const left = Date.parse(expiresAt) - now;
  if (Number.isNaN(left)) return `by ${expiresAt}`;
  if (left <= 0) return "after its deadline";
  if (left >= DAY_MS) return within(Math.floor(left / DAY_MS), "day");
  if (left >= HOUR_MS) return within(Math.floor(left / HOUR_MS), "hour");
  if (left >= MINUTE_MS) return within(Math.floor(left / MINUTE_MS), "minute");
  return "within a minute";
}

function within(count: number, unit: string): string {
  return `within ${count} ${unit}${count === 1 ? "" : "s"}`;
}

/** What to tell the person when a decision is refused. */
export function decideProblem(error: ApiError): string {
  switch (error.code) {
    case "proposal_not_pending":
      return "This proposal was already decided, perhaps in another tab, or its deadline passed. The list now shows where it stands.";
    case "proposal_not_found":
      return "The API no longer has this proposal. Reload the page.";
    default:
      return errorMessage(error);
  }
}
