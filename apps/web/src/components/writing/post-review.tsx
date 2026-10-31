import { ArrowRightIcon, LockIcon } from "lucide-react";
import Link from "next/link";

import { StatusPill } from "@/components/common/status-pill";
import type { ContentItem, Proposal } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { projectPageHref, proposalHref } from "@/lib/projects/project-navigation";
import { formatCost } from "@/lib/studio/status";
import { cn } from "@/lib/utils";
import { postLengthOf, type CheckReport, type Review } from "@/lib/writing/record";
import { postStatusDisplay, writingPlatformLabel } from "@/lib/writing/status";

import { CodeChecks, CriticReview, FlaggedNumbers } from "./post-checks";

type PostReviewProps = {
  post: ContentItem;
  check: CheckReport | null;
  review: Review | null;
  flaggedNumbers: readonly string[];
  /** The proposal the post waits on, when it is among those the screen read; null otherwise. */
  waitingOn: Proposal | null;
};

/**
 * The post the run proposed: the text exactly as it would be sent, its length on X, what became of
 * it, and what the code and the critic found of it. A post that waits is drawn as a draft, an
 * approved one with a solid border and a lock. The text was written by a model: shown as text only.
 */
export function PostReview({ post, check, review, flaggedNumbers, waitingOn }: PostReviewProps) {
  const status = postStatusDisplay(post.status);
  const length = postLengthOf(check);
  const approved = post.status === "dry_run";

  return (
    <section aria-labelledby="post" className="grid gap-5 border-t pt-8">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <h2 id="post" className="font-heading text-lg">
          The post
        </h2>
        <StatusPill tone={status.tone}>{status.label}</StatusPill>
      </div>
      <p className="max-w-prose text-sm text-pretty text-muted-foreground">{status.meaning}</p>

      <figure
        className={cn(
          "grid max-w-prose gap-3 rounded-md border p-5",
          status.draft ? "border-dashed border-warning/60" : approved ? "border-success/50 bg-card" : "bg-card",
        )}
      >
        {approved && (
          <p className="flex items-center gap-1.5 text-xs text-success">
            <LockIcon aria-hidden="true" className="size-3.5" />
            Approved{post.settled_at !== null && ` ${formatDateTime(post.settled_at)}`}
          </p>
        )}
        <blockquote className="text-base text-pretty break-words whitespace-pre-wrap">{post.text}</blockquote>
        <figcaption className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>{writingPlatformLabel(post.platform)}</span>
          {length === null ? (
            <span>Length not recorded</span>
          ) : (
            <span className={length.kept === false ? "text-destructive" : undefined}>
              Counts <span className="font-mono tabular-nums">{length.count}</span> as X counts it
              {length.kept === false && ", over its limit"}
              {length.kept === true && ", within its limit"}
            </span>
          )}
          <span>
            From profile version <span className="font-mono tabular-nums">{post.profile_version}</span>
          </span>
          <span>
            Cost <span className="font-mono tabular-nums">{formatCost(post.cost_usd)}</span>
          </span>
        </figcaption>
      </figure>

      {post.status === "proposed" && (
        <p className="text-sm">
          <Link
            href={
              waitingOn === null
                ? projectPageHref(post.project_id, "approvals")
                : proposalHref(post.project_id, waitingOn.id)
            }
            className="inline-flex items-center gap-1 rounded-sm text-brand underline-offset-4 hover:underline"
          >
            Approve or reject it in Approvals
            <ArrowRightIcon aria-hidden="true" className="size-3.5" />
          </Link>
        </p>
      )}

      <FlaggedNumbers numbers={flaggedNumbers} />

      <div className="grid max-w-prose gap-5">
        <CodeChecks report={check} />
        <CriticReview review={review} missing="No review by the critic was stored for this post." />
      </div>
    </section>
  );
}
