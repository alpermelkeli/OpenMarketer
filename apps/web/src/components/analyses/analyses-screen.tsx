"use client";

import { PlayIcon } from "lucide-react";

import { InlineCodeText } from "@/components/common/inline-code-text";
import { Notice } from "@/components/common/notice";
import { Button } from "@/components/ui/button";
import { startAnalysisProblem } from "@/lib/analysis/start-analysis-problem";
import { useStartAnalysis } from "@/lib/api/analyses";
import { withRun } from "@/lib/remembered/remembered";
import { useRemembered } from "@/lib/remembered/use-remembered";

import { AnalysisRun } from "./analysis-run";

type AnalysesScreenProps = { projectId: string };

/** Start an analysis of the project's repository and follow the runs started from this browser. */
export function AnalysesScreen({ projectId }: AnalysesScreenProps) {
  const { remembered, update } = useRemembered();
  const startAnalysis = useStartAnalysis(projectId);
  const runIds = remembered?.runIdsByProject[projectId] ?? [];
  const problem = startAnalysis.error === null ? null : startAnalysisProblem(startAnalysis.error);

  function start() {
    startAnalysis.mutate(undefined, {
      onSuccess: (run) => update((current) => withRun(current, projectId, run.id)),
    });
  }

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
        <Button size="lg" onClick={start} disabled={startAnalysis.isPending}>
          <PlayIcon data-icon="inline-start" aria-hidden="true" />
          {startAnalysis.isPending ? "Starting…" : "Start analysis"}
        </Button>
      </section>

      {problem !== null && (
        <Notice tone="danger" title={problem.title} role="alert">
          <InlineCodeText text={problem.text} />
        </Notice>
      )}

      <section aria-labelledby="runs" className="grid gap-4 border-t pt-8">
        <h2 id="runs" className="font-heading text-lg">
          Runs started here
        </h2>
        {remembered !== null && runIds.length === 0 && (
          <p className="max-w-prose text-sm text-pretty text-muted-foreground">
            No analysis has been started from this browser. If the project already has a profile, from the command
            line or an earlier run, it is on the Profile tab.
          </p>
        )}
        {runIds.map((runId) => (
          <AnalysisRun key={runId} projectId={projectId} runId={runId} />
        ))}
        <Notice className="max-w-prose">
          The API cannot list a project&rsquo;s runs yet. This page follows the runs started in this browser and reads
          each one&rsquo;s state from the API; runs started elsewhere are not shown.
        </Notice>
      </section>
    </div>
  );
}
