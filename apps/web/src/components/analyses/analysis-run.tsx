"use client";

import { Notice } from "@/components/common/notice";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { isRunFinished, isWaitingForWorker, runElapsedMs } from "@/lib/analysis/run-status";
import { useAnalysisRun } from "@/lib/api/analyses";
import { errorMessage } from "@/lib/api/error-message";
import { shortId } from "@/lib/format";
import { useNow } from "@/lib/use-now";

import { AnalysisRunCard } from "./analysis-run-card";

type AnalysisRunProps = { projectId: string; runId: string };

/** Follows one run on the server and shows it. */
export function AnalysisRun({ projectId, runId }: AnalysisRunProps) {
  const { data: run, error, refetch } = useAnalysisRun(projectId, runId);
  const now = useNow(run !== undefined && !isRunFinished(run));

  if (run !== undefined) {
    return (
      <AnalysisRunCard run={run} elapsedMs={runElapsedMs(run, now)} waitingForWorker={isWaitingForWorker(run, now)} />
    );
  }
  if (error !== null) {
    return (
      <Notice tone="danger" title={`Run ${shortId(runId)} could not be read`} role="status">
        <p>{error.code === "analysis_not_found" ? "The API has no such run for this project." : errorMessage(error)}</p>
        <Button variant="outline" size="sm" className="mt-3" onClick={() => void refetch()}>
          Try again
        </Button>
      </Notice>
    );
  }
  return <Skeleton className="h-28" />;
}
