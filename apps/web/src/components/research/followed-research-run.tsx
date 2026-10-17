"use client";

import { isRunFinished, isWaitingForWorker, runElapsedMs } from "@/lib/analysis/run-status";
import { useResearchRun } from "@/lib/api/research";
import type { ResearchRun } from "@/lib/api/types";
import { useNow } from "@/lib/use-now";

import { ResearchRunCard } from "./research-run-card";

type FollowedResearchRunProps = {
  projectId: string;
  /** The run as the list gave it: unfinished, so its state is asked for until it ends. */
  run: ResearchRun;
};

/** An unfinished research run, followed on the server and shown with a running clock. */
export function FollowedResearchRun({ projectId, run: listed }: FollowedResearchRunProps) {
  const { data } = useResearchRun(projectId, listed.id, listed);
  const run = data ?? listed;
  const now = useNow(!isRunFinished(run));
  return (
    <ResearchRunCard run={run} elapsedMs={runElapsedMs(run, now)} waitingForWorker={isWaitingForWorker(run, now)} />
  );
}
