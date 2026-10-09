"use client";

import { PlayIcon } from "lucide-react";

import { InlineCodeText } from "@/components/common/inline-code-text";
import { LoadMore } from "@/components/common/load-more";
import { Notice } from "@/components/common/notice";
import { RequestProblem } from "@/components/common/request-problem";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { isRunFinished, runElapsedMs } from "@/lib/analysis/run-status";
import { startAnalysisProblem } from "@/lib/analysis/start-analysis-problem";
import { useAnalyses, useStartAnalysis } from "@/lib/api/analyses";
import { useProject } from "@/lib/api/projects";
import { httpsUrlOrNull } from "@/lib/profile/evidence-link";

import { AnalysisRunCard } from "./analysis-run-card";
import { FollowedRun } from "./followed-run";

type AnalysesScreenProps = { projectId: string };

/** Start an analysis of the project's repository, follow it, and read the history of its runs. */
export function AnalysesScreen({ projectId }: AnalysesScreenProps) {
  const project = useProject(projectId);
  const analyses = useAnalyses(projectId);
  const startAnalysis = useStartAnalysis(projectId);

  const alreadyRunning = startAnalysis.error?.code === "analysis_already_running";
  const problem = startAnalysis.error === null ? null : startAnalysisProblem(startAnalysis.error);
  // The API analyses https repositories only. Said before the click; the refusal stays the API's.
  const notHttps = project.data !== undefined && httpsUrlOrNull(project.data.repository_url) === null;
  const runs = analyses.data;

  return (
    <div className="grid gap-8">
      <section aria-labelledby="start-analysis" className="flex flex-wrap items-start justify-between gap-x-10 gap-y-5">
        <div className="max-w-prose">
          <h2 id="start-analysis" className="font-heading text-title">
            Analyse the repository
          </h2>
          <p className="mt-2 text-pretty text-muted-foreground">
            A worker clones the repository, reads it and drafts a Product Profile with the evidence for each claim.
            It takes about a minute and calls a language model, which may cost money. The result is a draft: nothing
            is approved for you.
          </p>
        </div>
        <Button size="lg" onClick={() => startAnalysis.mutate()} disabled={startAnalysis.isPending}>
          <PlayIcon data-icon="inline-start" aria-hidden="true" />
          {startAnalysis.isPending ? "Starting…" : "Start analysis"}
        </Button>
      </section>

      {notHttps && (
        <Notice tone="warning" title="This repository is not an https:// address">
          The API analyses only repositories it can clone over https, so it will refuse to start an analysis of this
          project. A project stored from a local folder by the command line is analysed there, with{" "}
          <code>make analyze</code>.
        </Notice>
      )}

      {problem !== null && (
        <Notice tone={alreadyRunning ? "progress" : "danger"} title={problem.title} role="alert">
          <InlineCodeText text={problem.text} />
        </Notice>
      )}

      <section aria-labelledby="runs" className="grid gap-4 border-t pt-8">
        <h2 id="runs" className="font-heading text-lg">
          Runs
        </h2>
        {analyses.error !== null && (
          <RequestProblem
            title="The runs could not be read"
            error={analyses.error}
            onRetry={() => void analyses.refetch()}
          />
        )}
        {runs === undefined && analyses.error === null && <Skeleton className="h-28" />}
        {runs?.length === 0 && (
          <p className="max-w-prose text-sm text-pretty text-muted-foreground">
            No analysis has been requested for this project through the API. A profile stored by the command line has
            no run; it is on the Profile tab.
          </p>
        )}
        {runs?.map((run) =>
          isRunFinished(run) ? (
            <AnalysisRunCard key={run.id} run={run} elapsedMs={runElapsedMs(run, 0)} waitingForWorker={false} />
          ) : (
            <FollowedRun key={run.id} projectId={projectId} run={run} />
          ),
        )}
        <LoadMore
          noun="runs"
          hasMore={analyses.hasNextPage}
          loading={analyses.isFetchingNextPage}
          onLoadMore={() => void analyses.fetchNextPage()}
        />
      </section>
    </div>
  );
}
