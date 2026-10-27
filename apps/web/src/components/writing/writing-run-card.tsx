import { ArrowRightIcon } from "lucide-react";
import Link from "next/link";

import { Notice } from "@/components/common/notice";
import { StatusPill } from "@/components/common/status-pill";
import { formatDuration, runElapsedMs } from "@/lib/analysis/run-status";
import type { WritingRun } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";
import { writingHref } from "@/lib/projects/project-navigation";
import { formatCost, requestedByLabel } from "@/lib/studio/status";
import {
  isWritingRunFinished,
  isWritingRunWaitingForWorker,
  writingPlatformLabel,
  writingRunStatusDisplay,
} from "@/lib/writing/status";

type WritingRunCardProps = {
  run: WritingRun;
  now: number;
  /** In a list, the card leads to the run's own page; on that page it does not. */
  linked: boolean;
};

/** One writing run: where it stands, how long it took, what it cost, and why it ended as it did. */
export function WritingRunCard({ run, now, linked }: WritingRunCardProps) {
  const status = writingRunStatusDisplay(run);
  const finished = isWritingRunFinished(run);
  const waitingForWorker = isWritingRunWaitingForWorker(run, now);
  const waitingForDecision = run.status === "running" && run.content_item_id !== null;

  return (
    <article aria-label={`Writing run ${shortId(run.id)}`} className="grid gap-4 rounded-md border bg-card p-5">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <StatusPill tone={status.tone} live={!finished && !waitingForDecision}>
          {status.label}
        </StatusPill>
        <p className="text-sm">{writingPlatformLabel(run.platform)}</p>
        <p className="text-sm text-muted-foreground">
          Requested {formatDateTime(run.created_at)} by {requestedByLabel(run.requested_by)}
        </p>
        <p className="ml-auto flex gap-3 font-mono text-sm tabular-nums">
          <span aria-label={finished ? "Time taken" : "Time elapsed"}>{formatDuration(runElapsedMs(run, now))}</span>
          {finished && <span aria-label="Cost">{formatCost(run.cost_usd)}</span>}
        </p>
      </div>

      {/* Polite, so a screen reader hears the run move on without being interrupted. */}
      <div aria-live="polite" className="grid gap-3 text-sm">
        {run.status === "queued" && !waitingForWorker && (
          <p className="text-muted-foreground">Waiting for a worker to pick it up.</p>
        )}
        {run.status === "queued" && waitingForWorker && (
          <Notice tone="warning" title="Still queued. Is a worker running?">
            A run waits in the queue until a worker takes it, and it will wait for as long as there is none. Start
            one with <code>make worker</code>; this page keeps checking and will carry on by itself.
          </Notice>
        )}
        {run.status === "running" && !waitingForDecision && (
          <p className="max-w-prose text-pretty text-muted-foreground">
            Planning the post, writing the texts, checking them in code and having the critic read them. This usually
            takes a minute or two; this page checks every two seconds.
          </p>
        )}
        {waitingForDecision && (
          <p className="max-w-prose text-pretty text-muted-foreground">
            The post is written and waits in Approvals for you to approve or reject it. Nothing has been sent.
          </p>
        )}
        {run.status === "failed" && (
          <Notice tone="danger" title="The run failed" role="status">
            {/* The reason is recorded by the worker and is text only. */}
            <p className="font-mono text-xs break-words whitespace-pre-wrap text-foreground">
              {run.error ?? "No reason was recorded."}
            </p>
            <p className="mt-2">Nothing was sent. Another run can be started with a new brief.</p>
          </Notice>
        )}
        {linked && (
          <p>
            <Link
              href={writingHref(run.project_id, run.id)}
              className="inline-flex items-center gap-1 rounded-sm text-brand underline-offset-4 hover:underline"
            >
              {run.content_item_id === null ? "Open the run" : "Read the post and how it was written"}
              <ArrowRightIcon aria-hidden="true" className="size-3.5" />
            </Link>
          </p>
        )}
      </div>

      <p className="font-mono text-2xs text-muted-foreground">Run {run.id}</p>
    </article>
  );
}
