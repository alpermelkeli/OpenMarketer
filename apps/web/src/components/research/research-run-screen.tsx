"use client";

import { ArrowLeftIcon } from "lucide-react";
import Link from "next/link";

import { Notice } from "@/components/common/notice";
import { RequestProblem } from "@/components/common/request-problem";
import { Skeleton } from "@/components/ui/skeleton";
import { isRunFinished, isWaitingForWorker, runElapsedMs } from "@/lib/analysis/run-status";
import { useResearchReport, useResearchRun } from "@/lib/api/research";
import { projectPageHref } from "@/lib/projects/project-navigation";
import { useNow } from "@/lib/use-now";

import { ResearchReportView } from "./research-report";
import { ResearchRunCard } from "./research-run-card";

type ResearchRunScreenProps = { projectId: string; runId: string };

/** One research run: how it is going, and its report once it has one. */
export function ResearchRunScreen({ projectId, runId }: ResearchRunScreenProps) {
  const run = useResearchRun(projectId, runId);
  const finished = run.data !== undefined && isRunFinished(run.data);
  const now = useNow(!finished);
  const report = useResearchReport(projectId, runId, run.data?.status === "succeeded");

  if (run.error !== null) {
    const missing = run.error.code === "research_not_found";
    return (
      <div className="grid gap-8">
        <BackToResearch projectId={projectId} />
        {missing ? (
          <Notice tone="warning" title="No such research run">
            <p>This project has no research run with this ID. The address may name a run of another project.</p>
          </Notice>
        ) : (
          <RequestProblem title="The run could not be read" error={run.error} onRetry={() => void run.refetch()} />
        )}
      </div>
    );
  }

  if (run.data === undefined) {
    return (
      <div className="grid gap-8">
        <BackToResearch projectId={projectId} />
        <Skeleton className="h-48" />
      </div>
    );
  }

  const found = run.data;
  return (
    <div className="grid gap-8">
      <BackToResearch projectId={projectId} />
      <ResearchRunCard
        run={found}
        elapsedMs={runElapsedMs(found, now)}
        waitingForWorker={isWaitingForWorker(found, now)}
      />
      {found.status === "succeeded" && (
        <>
          {report.error !== null && (
            <RequestProblem
              title="The report could not be read"
              error={report.error}
              onRetry={() => void report.refetch()}
            />
          )}
          {report.data === undefined && report.error === null && <Skeleton className="h-48" />}
          {report.data !== undefined && <ResearchReportView report={report.data} />}
        </>
      )}
    </div>
  );
}

/** The way back to the project's research runs, since a run's page hides the list. */
function BackToResearch({ projectId }: { projectId: string }) {
  return (
    <Link
      href={projectPageHref(projectId, "research")}
      className="inline-flex w-fit items-center gap-1.5 rounded-sm text-sm text-muted-foreground hover:text-foreground"
    >
      <ArrowLeftIcon aria-hidden="true" className="size-3.5" />
      All research
    </Link>
  );
}
