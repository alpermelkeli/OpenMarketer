/**
 * How a proposal is shown: its state in words and a tone, what its action does, what it asks,
 * and how long a person has. Display only: whether an action may run is the server's decision.
 */

import type { Tone } from "@/lib/analysis/run-status";
import type { ApiError } from "@/lib/api/api-error";
import { errorMessage } from "@/lib/api/error-message";
import type { ActionEffect, PostDestination, Proposal, ProposalStatus } from "@/lib/api/types";

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
  confirms: "Confirm a plan",
};

export function effectLabel(effect: ActionEffect): string {
  return EFFECT_LABEL[effect];
}

const EFFECT_NOTE: Record<ActionEffect, string> = {
  publishes: "It puts something in front of other people.",
  spends: "It costs money.",
  internal: "It works for the run itself.",
  confirms: "The task waits for this before it runs anything.",
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
  ["confirm_plan", "Confirm a task's plan"],
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

/**
 * Where a writer's post goes once approved, as a sentence to read before deciding: "Publishes from
 * @name on X · about $0.015", or a dry run that sends nothing. `live` is the server's word for it;
 * the username is the platform's own, read when the account was connected. Null for a proposal
 * that is not a writer's post. Accounts are connected on X only, so X is named.
 */
export function postDestinationDisplay(destination: PostDestination | null): { live: boolean; text: string } | null {
  if (destination === null) return null;
  if (!destination.live) return { live: false, text: "Dry run: no X account connected — nothing will be posted" };
  const account = destination.username === null ? "the connected account" : `@${destination.username}`;
  const cost = destination.cost_estimate_usd === null ? "" : ` · about ${ESTIMATE.format(destination.cost_estimate_usd)}`;
  return { live: true, text: `Publishes from ${account} on X${cost}` };
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

/**
 * The words of the two buttons that decide a proposal: a plan to confirm is confirmed or
 * rejected, a post that would be published is approved and published, any other action approved
 * or rejected.
 */
export function decisionLabels(
  effect: string,
  destination: PostDestination | null = null,
): { approve: string; reject: string } {
  if (effect === "confirms") return { approve: "Confirm the plan", reject: "Reject the plan" };
  if (destination?.live === true) return { approve: "Approve and publish", reject: "Reject" };
  return { approve: "Approve", reject: "Reject" };
}

/**
 * What the step before deciding on a writer's post says: where approving sends it, or that it is
 * a dry run, and that a rejection sends nothing. Null for a proposal that is not a writer's post.
 */
export function postDecisionWording(
  destination: PostDestination | null,
  approve: boolean,
): { title: string; description: string; action: string } | null {
  const display = postDestinationDisplay(destination);
  if (display === null) return null;
  if (!approve) {
    return {
      title: "Reject this post?",
      description: "The post is not sent anywhere, and its run ends.",
      action: "Reject",
    };
  }
  if (!display.live) {
    return {
      title: "Approve this post as a dry run?",
      description:
        "No X account was connected when the post was proposed, so it is recorded as it would have been sent and nothing is posted.",
      action: "Approve",
    };
  }
  return {
    title: "Publish this post?",
    description: `${display.text}. The run publishes it exactly as shown, with its image when it has one. Once it is on X, it cannot be taken back from here.`,
    action: "Approve and publish",
  };
}

/**
 * What the step before deciding on a task's plan says: what follows a confirmation, which runs the
 * steps, and a rejection, which ends the task. A post the plan writes still waits for its own
 * decision; that is the gate's rule, which this only repeats.
 */
export function planDecisionWording(approve: boolean): { title: string; description: string; action: string } {
  return approve
    ? {
        title: "Confirm this plan?",
        description:
          "The task runs its steps as shown. A studio step can spend from the creative budget; a post a writing step proposes still waits for your own decision before anything is sent.",
        action: "Confirm the plan",
      }
    : {
        title: "Reject this plan?",
        description: "The task ends without running any of its steps, and reports so in its chat.",
        action: "Reject the plan",
      };
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
