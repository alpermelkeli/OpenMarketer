"use client";

import Link from "next/link";

import { LoadMore } from "@/components/common/load-more";
import { RequestProblem } from "@/components/common/request-problem";
import { Skeleton } from "@/components/ui/skeleton";
import { isRunFinished, runElapsedMs } from "@/lib/analysis/run-status";
import { useResearches } from "@/lib/api/research";
import { projectPageHref } from "@/lib/projects/project-navigation";

import { FollowedResearchRun } from "./followed-research-run";
import { ResearchRunCard } from "./research-run-card";

type ResearchScreenProps = { projectId: string };

/** The project's research runs: what each answers, where it stands, and how to read its report. */
export function ResearchScreen({ projectId }: ResearchScreenProps) {
  const researches = useResearches(projectId);
  const runs = researches.data;

  return (
    <div className="grid gap-8">
      <section aria-labelledby="about-research" className="max-w-prose">
        <h2 id="about-research" className="font-heading text-title">
          Research the market
        </h2>
        <p className="mt-2 text-pretty text-muted-foreground">
          A research run answers questions from public pages. It plans its own queries, searches, fetches and reads
          what it finds, and stores a report of findings, each with the pages it rests on and the quotes that
          support it. Start one from{" "}
          <Link
            href={projectPageHref(projectId, "chat")}
            className="rounded-sm text-brand underline-offset-4 hover:underline"
          >
            a chat with the orchestrator
          </Link>
          , which asks for the questions it needs.
        </p>
      </section>

      <section aria-labelledby="runs" className="grid gap-4 border-t pt-8">
        <h2 id="runs" className="font-heading text-lg">
          Runs
        </h2>
        {researches.error !== null && (
          <RequestProblem
            title="The runs could not be read"
            error={researches.error}
            onRetry={() => void researches.refetch()}
          />
        )}
        {runs === undefined && researches.error === null && <Skeleton className="h-28" />}
        {runs?.length === 0 && (
          <p className="max-w-prose text-sm text-pretty text-muted-foreground">
            No research has been requested for this project yet. Ask the orchestrator for one in a chat.
          </p>
        )}
        {runs?.map((run) =>
          isRunFinished(run) ? (
            <ResearchRunCard key={run.id} run={run} elapsedMs={runElapsedMs(run, 0)} waitingForWorker={false} />
          ) : (
            <FollowedResearchRun key={run.id} projectId={projectId} run={run} />
          ),
        )}
        <LoadMore
          noun="runs"
          hasMore={researches.hasNextPage}
          loading={researches.isFetchingNextPage}
          onLoadMore={() => void researches.fetchNextPage()}
        />
      </section>
    </div>
  );
}
