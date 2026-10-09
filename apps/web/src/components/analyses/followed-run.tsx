"use client";

import { isRunFinished, isWaitingForWorker, runElapsedMs } from "@/lib/analysis/run-status";
import { useAnalysisRun } from "@/lib/api/analyses";
import type { AnalysisRun } from "@/lib/api/types";
import { useNow } from "@/lib/use-now";

import { AnalysisRunCard } from "./analysis-run-card";

type FollowedRunProps = {
  projectId: string;
  /** The run as the list gave it: unfinished, so its state is asked for until it ends. */
  run: AnalysisRun;
};

/** An unfinished run, followed on the server and shown with a running clock. */
export function FollowedRun({ projectId, run: listed }: FollowedRunProps) {
  const { data: run } = useAnalysisRun(projectId, listed);
  const now = useNow(!isRunFinished(run));
  return (
    <AnalysisRunCard run={run} elapsedMs={runElapsedMs(run, now)} waitingForWorker={isWaitingForWorker(run, now)} />
  );
}
