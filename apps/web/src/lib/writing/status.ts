/**
 * How the writer's runs and posts are shown: a state in words with a tone, what a brief asked for,
 * and which proposal a waiting post is decided on. Display only: whether a run is done, and
 * whether a post is sent, is the server's to say.
 */

import { QUEUED_TOO_LONG_MS, runElapsedMs, type Tone } from "@/lib/analysis/run-status";
import type {
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

const POST_STATUS: Record<ContentItemStatus, PostStatusDisplay> = {
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
      "A person approved it and it was recorded as it would have been sent. The writer sends nothing to X yet.",
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
    meaning: "The approved post could not be recorded. It was not sent.",
    draft: false,
  },
};

export function postStatusDisplay(status: ContentItemStatus): PostStatusDisplay {
  return POST_STATUS[status];
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

const PLATFORMS: Record<WritingPlatform, string> = { x: "X" };

/** The platform's name; a slug the dashboard does not know yet is shown as it came. */
export function writingPlatformLabel(platform: string): string {
  return Object.hasOwn(PLATFORMS, platform) ? PLATFORMS[platform as WritingPlatform] : platform;
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
