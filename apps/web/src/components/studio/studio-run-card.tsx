import { Notice } from "@/components/common/notice";
import { StatusPill } from "@/components/common/status-pill";
import { formatDuration, runElapsedMs } from "@/lib/analysis/run-status";
import type { StudioRun } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import {
  formatCost,
  isStudioRunFinished,
  isStudioRunWaitingForWorker,
  platformLabel,
  requestedByLabel,
  studioRunStatusDisplay,
} from "@/lib/studio/status";

import { DeleteStudioRunButton } from "./delete-studio-run-button";

type StudioRunCardProps = {
  run: StudioRun;
  now: number;
  onDeleted: () => void;
};

/** One studio run: where it stands, how long it took, what it cost, and why it ended as it did. */
export function StudioRunCard({ run, now, onDeleted }: StudioRunCardProps) {
  const status = studioRunStatusDisplay(run.status);
  const finished = isStudioRunFinished(run);
  const waitingForWorker = isStudioRunWaitingForWorker(run, now);

  return (
    <section aria-label="The run" className="grid gap-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <StatusPill tone={status.tone} live={!finished}>
          {status.label}
        </StatusPill>
        <p className="text-sm">{platformLabel(run.platform)}</p>
        <p className="text-sm text-muted-foreground">
          Requested {formatDateTime(run.created_at)} by {requestedByLabel(run.requested_by)}
        </p>
        <p className="ml-auto flex gap-3 font-mono text-sm tabular-nums">
          <span aria-label={finished ? "Time taken" : "Time elapsed"}>{formatDuration(runElapsedMs(run, now))}</span>
          <span aria-label="Cost">{formatCost(run.cost_usd)}</span>
        </p>
        <DeleteStudioRunButton run={run} onDeleted={onDeleted} />
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
        {run.status === "running" && (
          <p className="max-w-prose text-pretty text-muted-foreground">
            Making candidates, checking them and editing them. An image that costs more than the automatic limit
            waits in Approvals for you first. This page checks every two seconds.
          </p>
        )}
        {run.status === "needs_review" && (
          <Notice tone="warning" title="The run reached its limits">
            It used its rounds or its budget before a candidate passed every check. The image below is its best
            candidate, with what failed written under each version. Accept it only if what failed does not matter to
            you.
          </Notice>
        )}
        {run.status === "failed" && (
          <Notice tone="danger" title="The run failed" role="status">
            {/* The reason is recorded by the worker and is text only. */}
            <p className="font-mono text-xs break-words whitespace-pre-wrap text-foreground">
              {run.error ?? "No reason was recorded."}
            </p>
            <p className="mt-2">The run ended without an image for you to review. Another can be asked for.</p>
          </Notice>
        )}
      </div>

      <p className="font-mono text-2xs text-muted-foreground">Run {run.id}</p>
    </section>
  );
}
