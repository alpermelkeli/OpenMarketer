"use client";

import { ArrowLeftIcon } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Notice } from "@/components/common/notice";
import { RequestProblem } from "@/components/common/request-problem";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api/error-message";
import { useDecideOnAsset, useMediaAsset, useStudioRun } from "@/lib/api/studio";
import { projectPageHref } from "@/lib/projects/project-navigation";
import { briefGoal, runBrief } from "@/lib/studio/brief";
import { versionsCost } from "@/lib/studio/reports";
import { isStudioRunFinished } from "@/lib/studio/status";
import { useNow } from "@/lib/use-now";

import { AssetReview } from "./asset-review";
import { RunBrief } from "./run-brief";
import { StudioRunCard } from "./studio-run-card";
import { VersionRounds } from "./version-rounds";

type StudioRunScreenProps = { projectId: string; runId: string };

/** One studio run: how it is going, the image it ended with and the decision on it, and every round. */
export function StudioRunScreen({ projectId, runId }: StudioRunScreenProps) {
  const router = useRouter();
  const run = useStudioRun(projectId, runId);
  const finished = run.data !== undefined && isStudioRunFinished(run.data);
  const now = useNow(!finished);
  const assetId = run.data?.final_asset_id ?? null;
  const detail = useMediaAsset(projectId, assetId);
  const decide = useDecideOnAsset(projectId);
  const [rejectOpen, setRejectOpen] = useState(false);

  if (run.error !== null) {
    const missing = run.error.code === "studio_run_not_found";
    return (
      <div className="grid gap-8">
        <BackToStudio projectId={projectId} />
        {missing ? (
          <Notice tone="warning" title="No such studio run">
            <p>This project has no studio run with this ID. The address may name a run of another project.</p>
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
        <BackToStudio projectId={projectId} />
        <Skeleton className="h-48" />
      </div>
    );
  }

  const found = run.data;
  const brief = runBrief(found);
  const goal = briefGoal(brief);
  const asset = detail.data?.asset;
  const versions = detail.data?.versions ?? [];
  const current = versions.find((version) => version.id === asset?.current_version_id) ?? null;
  const deciding = decide.isPending ? decide.variables.decision : null;

  function decideOn(decision: "accept" | "reject") {
    if (asset === undefined) return;
    decide.mutate({ assetId: asset.id, decision }, { onSuccess: () => setRejectOpen(false) });
  }

  return (
    <div className="grid gap-8">
      <BackToStudio projectId={projectId} />
      <StudioRunCard
        run={found}
        now={now}
        onDeleted={() => router.replace(projectPageHref(projectId, "studio"))}
      />

      <RunBrief brief={brief} />

      {assetId !== null && (
        <>
          {detail.error !== null && (
            <RequestProblem
              title="The image could not be read"
              error={detail.error}
              onRetry={() => void detail.refetch()}
            />
          )}
          {detail.data === undefined && detail.error === null && <Skeleton className="h-96" />}
          {asset !== undefined && (
            <>
              <AssetReview
                asset={asset}
                goal={goal}
                version={current}
                totalCostUsd={versionsCost(versions)}
                decidable={finished}
                deciding={deciding}
                problem={decide.error === null ? null : errorMessage(decide.error)}
                onAccept={() => {
                  decide.reset();
                  decideOn("accept");
                }}
                onReject={() => decideOn("reject")}
                rejectOpen={rejectOpen}
                onRejectOpenChange={(open) => {
                  decide.reset();
                  setRejectOpen(open);
                }}
              />
              <VersionRounds
                projectId={projectId}
                assetId={asset.id}
                platform={asset.platform}
                goal={goal}
                versions={versions}
                currentVersionId={asset.current_version_id}
              />
            </>
          )}
        </>
      )}
    </div>
  );
}

/** The way back to the project's library, since a run's page hides it. */
function BackToStudio({ projectId }: { projectId: string }) {
  return (
    <Link
      href={projectPageHref(projectId, "studio")}
      className="inline-flex w-fit items-center gap-1.5 rounded-sm text-sm text-muted-foreground hover:text-foreground"
    >
      <ArrowLeftIcon aria-hidden="true" className="size-3.5" />
      The whole library
    </Link>
  );
}
