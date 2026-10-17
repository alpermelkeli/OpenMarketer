import { ArrowRightIcon } from "lucide-react";
import Link from "next/link";

import { Notice } from "@/components/common/notice";
import { StatusPill } from "@/components/common/status-pill";
import { formatDuration, isRunFinished, runStatusDisplay } from "@/lib/analysis/run-status";
import type { ResearchRun } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";
import { researchHref } from "@/lib/projects/project-navigation";

type ResearchRunCardProps = {
  run: ResearchRun;
  elapsedMs: number;
  /** The run has been queued long enough that a missing worker is the likeliest reason. */
  waitingForWorker: boolean;
};

const REQUESTED_BY: Record<string, string> = {
  person: "a person",
  orchestrator: "the orchestrator chat",
  run: "another run",
};

/** One research run: where it stands, how long it has taken, and what came of it. */
export function ResearchRunCard({ run, elapsedMs, waitingForWorker }: ResearchRunCardProps) {
  const status = runStatusDisplay(run.status);
  const finished = isRunFinished(run);

  return (
    <article aria-label={`Research ${shortId(run.id)}`} className="grid gap-4 rounded-md border bg-card p-5">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <StatusPill tone={status.tone} live={!finished}>
          {status.label}
        </StatusPill>
        <p className="text-sm text-muted-foreground">
          Requested {formatDateTime(run.created_at)} by {REQUESTED_BY[run.requested_by] ?? run.requested_by}
        </p>
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
            Planning queries, searching and reading public pages. This usually takes a few minutes; this page
            checks every two seconds.
          </p>
        )}
        {run.status === "failed" && (
          <Notice tone="danger" title="The research failed" role="status">
            {/* The reason is recorded by the worker and is text only. */}
            <p className="font-mono text-xs break-words whitespace-pre-wrap text-foreground">
              {run.error ?? "No reason was recorded."}
            </p>
            <p className="mt-2">The project is not blocked by a failed run. Another can be asked for from a chat.</p>
          </Notice>
        )}
        {run.status === "succeeded" && (
          <p className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span>Stored a report of findings, each with the pages it rests on.</span>
            <Link
              href={researchHref(run.project_id, run.id)}
              className="inline-flex items-center gap-1 rounded-sm text-brand underline-offset-4 hover:underline"
            >
              Read the report
              <ArrowRightIcon aria-hidden="true" className="size-3.5" />
            </Link>
          </p>
        )}
      </div>

      <p className="font-mono text-2xs text-muted-foreground">Run {run.id}</p>
    </article>
  );
}
