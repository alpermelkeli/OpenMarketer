import { ArrowRightIcon } from "lucide-react";
import Link from "next/link";

import { Notice } from "@/components/common/notice";
import { StatusPill } from "@/components/common/status-pill";
import { formatDuration, isRunFinished, runStatusDisplay } from "@/lib/analysis/run-status";
import type { AnalysisRun } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";

type AnalysisRunCardProps = {
  run: AnalysisRun;
  elapsedMs: number;
  /** The run has been queued long enough that a missing worker is the likeliest reason. */
  waitingForWorker: boolean;
};

/** One analysis run: where it stands, how long it has taken, and what came of it. */
export function AnalysisRunCard({ run, elapsedMs, waitingForWorker }: AnalysisRunCardProps) {
  const status = runStatusDisplay(run.status);
  const finished = isRunFinished(run);

  return (
    <article aria-label={`Analysis ${shortId(run.id)}`} className="grid gap-4 rounded-md border bg-card p-5">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <StatusPill tone={status.tone} live={!finished}>
          {status.label}
        </StatusPill>
        <p className="text-sm text-muted-foreground">Requested {formatDateTime(run.created_at)}</p>
        <p className="ml-auto font-mono text-sm tabular-nums" aria-label={finished ? "Time taken" : "Time elapsed"}>
          {formatDuration(elapsedMs)}
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
        {run.status === "running" && (
          <p className="text-muted-foreground">
            Cloning the repository, reading it and drafting the profile. This usually takes about a minute; this
            page checks every two seconds.
          </p>
        )}
        {run.status === "failed" && (
          <Notice tone="danger" title="The analysis failed" role="status">
            {/* The reason is recorded by the worker and may quote the repository: text only. */}
            <p className="font-mono text-xs break-words whitespace-pre-wrap text-foreground">
              {run.error ?? "No reason was recorded."}
            </p>
            <p className="mt-2">A failed run does not block the project. You can start another.</p>
          </Notice>
        )}
        {run.status === "succeeded" && (
          <p className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span>
              Stored a draft as profile version{" "}
              <span className="font-mono tabular-nums">{run.profile_version ?? "?"}</span>.
            </span>
            <Link
              href={
                run.profile_version === null
                  ? `/projects/${run.project_id}/profile`
                  : `/projects/${run.project_id}/profile?version=${run.profile_version}`
              }
              className="inline-flex items-center gap-1 rounded-sm text-brand underline-offset-4 hover:underline"
            >
              {run.profile_version === null ? "Review the profile" : `Open version ${run.profile_version}`}
              <ArrowRightIcon aria-hidden="true" className="size-3.5" />
            </Link>
          </p>
        )}
      </div>

      <p className="font-mono text-2xs text-muted-foreground">Run {run.id}</p>
    </article>
  );
}
