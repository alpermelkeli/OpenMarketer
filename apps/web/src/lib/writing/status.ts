/**
 * How the writer's runs and posts are shown: a state in words with a tone, what a brief asked for,
 * and which proposal a waiting post is decided on. Display only: whether a run is done, and
 * whether a post is sent, is the server's to say. What became of a post that was sent is in
 * `publication.ts`.
 */

import { QUEUED_TOO_LONG_MS, runElapsedMs, type Tone } from "@/lib/analysis/run-status";
import type {
  ContentItem,
  ContentItemStatus,
  Proposal,
  WritingGoal,
  WritingPlatform,
  WritingRun,
  WritingRunStatus,
} from "@/lib/api/types";

type StatusDisplay = { label: string; tone: Tone };

const RUN_STATUS: Record<WritingRunStatus, StatusDisplay> = {
  queued: { label: "Queued", tone: "neutral" },
  running: { label: "Writing", tone: "progress" },
  succeeded: { label: "Finished", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
};

/**
 * The run's state. A running run that has stored its post is waiting for a person's decision on
 * it, which is what the person needs to know, so it is said so.
 */
export function writingRunStatusDisplay(run: Pick<WritingRun, "status" | "content_item_id">): StatusDisplay {
  if (run.status === "running" && run.content_item_id !== null) {
    return { label: "Waiting for your decision", tone: "warning" };
  }
  return RUN_STATUS[run.status];
}

/** A run that has ended never changes again, so it is no longer polled. */
export function isWritingRunFinished(run: Pick<WritingRun, "status">): boolean {
  return run.status === "succeeded" || run.status === "failed";
}

/** After a while in the queue, the likeliest reason is that no worker is running. */
export function isWritingRunWaitingForWorker(
  run: Pick<WritingRun, "status" | "created_at" | "finished_at">,
  now: number,
): boolean {
  return run.status === "queued" && runElapsedMs(run, now) >= QUEUED_TOO_LONG_MS;
}

type PostStatusDisplay = StatusDisplay & {
  /** What became of the post, in a sentence. */
  meaning: string;
  /** A post that still waits is a draft and is drawn as one (a dashed border). */
  draft: boolean;
};

/** Every state of a post, its platform named where the post was sent to it. */
function postStatuses(platform: string): Record<ContentItemStatus, PostStatusDisplay> {
  const on = writingPlatformLabel(platform);
  return {
    proposed: {
      label: "Waiting for your decision",
      tone: "warning",
      meaning: "Nothing has been sent. The post waits for a person to approve or reject it in Approvals.",
      draft: true,
    },
    dry_run: {
      label: "Approved, dry run",
      tone: "success",
      meaning:
        "A person approved it and it was recorded as it would have been sent. No account was connected when it was proposed, so nothing was posted.",
      draft: false,
    },
    publishing: {
      label: `Publishing on ${on}…`,
      tone: "progress",
      meaning: `A person approved it and it is being sent to the account on ${on} now.`,
      draft: false,
    },
    published: {
      label: `Published on ${on}`,
      tone: "success",
      meaning: `A person approved it and ${on} published it.`,
      draft: false,
    },
    rejected: {
      label: "Rejected",
      tone: "danger",
      meaning: "A person rejected it. It was not sent, and the run ended.",
      draft: false,
    },
    expired: {
      label: "Expired",
      tone: "neutral",
      meaning: "Nobody decided before its deadline, so it was not sent.",
      draft: false,
    },
    failed: {
      label: "Failed",
      tone: "danger",
      meaning: "The post was not published.",
      draft: false,
    },
  };
}

/**
 * A send the run never confirmed: it started and the run ended with no outcome recorded. The
 * platform may have the post or not, so it is neither a failure nor published: what the reader
 * has to check.
 */
const UNCONFIRMED: PostStatusDisplay = {
  label: "Not confirmed",
  tone: "warning",
  meaning: "Publishing started, but the run ended before the platform's answer was recorded.",
  draft: false,
};

export function postStatusDisplay(
  post: Pick<ContentItem, "status" | "platform" | "publishing_unconfirmed">,
): PostStatusDisplay {
  if (post.publishing_unconfirmed) return UNCONFIRMED;
  return postStatuses(post.platform)[post.status];
}

const APPROVED: readonly ContentItemStatus[] = ["dry_run", "publishing", "published"];

/** Whether a person approved the post: drawn with a solid border and a lock. */
export function isPostApproved(post: Pick<ContentItem, "status">): boolean {
  return APPROVED.includes(post.status);
}

/** Whether a post is being sent right now: shown as work in progress. */
export function isPostPublishing(post: Pick<ContentItem, "status" | "publishing_unconfirmed">): boolean {
  return post.status === "publishing" && !post.publishing_unconfirmed;
}

const GOALS: Record<WritingGoal, string> = {
  introduce: "Introduce the product",
  announce: "Announce a live feature",
  angle: "Write from your angle",
};

/** Every goal a brief can have, in the order a form offers them. */
export const GOAL_OPTIONS: readonly { value: WritingGoal; label: string }[] = (Object.keys(GOALS) as WritingGoal[]).map(
  (value) => ({ value, label: GOALS[value] }),
);

/** The goal in words; one the dashboard does not know yet is shown as it came. */
export function goalLabel(goal: string): string {
  return Object.hasOwn(GOALS, goal) ? GOALS[goal as WritingGoal] : goal;
}

const PLATFORMS: Record<WritingPlatform, string> = { x: "X", instagram: "Instagram" };

/** Every platform the writer writes for, in the order a form offers them. */
export const PLATFORM_OPTIONS: readonly { value: WritingPlatform; label: string }[] = (
  Object.keys(PLATFORMS) as WritingPlatform[]
).map((value) => ({ value, label: PLATFORMS[value] }));

/** The platform's name; a slug the dashboard does not know yet is shown as it came. */
export function writingPlatformLabel(platform: string): string {
  return Object.hasOwn(PLATFORMS, platform) ? PLATFORMS[platform as WritingPlatform] : platform;
}

/** "Counts 212 as X counts it": how the check measured a post's length on its platform. */
export function countedAs(platform: string): string {
  return `as ${writingPlatformLabel(platform)} counts it`;
}

/** The action a writing run asks a person to approve, by the name its agent declares. */
export const PUBLISH_POST = "publish_post";

/**
 * The proposal the run's post waits on, among those that wait for a person: the post's own
 * content item does not name its proposal until the decision is carried out, so it is found by its
 * run. None when nothing of the run waits.
 */
export function waitingPostProposal(waiting: readonly Proposal[], runId: string): Proposal | null {
  return (
    waiting.find(
      (proposal) => proposal.status === "pending" && proposal.run_id === runId && proposal.action_name === PUBLISH_POST,
    ) ?? null
  );
}
