"use client";

import { ArrowLeftIcon } from "lucide-react";
import Link from "next/link";

import { Notice } from "@/components/common/notice";
import { RequestProblem } from "@/components/common/request-problem";
import { Skeleton } from "@/components/ui/skeleton";
import { useProfileVersion } from "@/lib/api/profiles";
import { useProject } from "@/lib/api/projects";
import { useProposals } from "@/lib/api/proposals";
import { useContentItem, useWritingRun } from "@/lib/api/writing";
import type { ContentItemDetail } from "@/lib/api/types";
import type { EvidenceSource } from "@/lib/profile/evidence-link";
import { projectPageHref } from "@/lib/projects/project-navigation";
import { useNow } from "@/lib/use-now";
import { runBrief } from "@/lib/writing/brief";
import { alternativesOf, checkReportOf, claimsOf, planOf, reviewOf } from "@/lib/writing/record";
import { isWritingRunFinished, waitingPostProposal } from "@/lib/writing/status";

import { PostAlternatives } from "./post-alternatives";
import { PostClaims } from "./post-claims";
import { PostPlan } from "./post-plan";
import { PostReview } from "./post-review";
import { RunBrief } from "./run-brief";
import { WritingRunCard } from "./writing-run-card";

type WritingRunScreenProps = { projectId: string; runId: string };

/**
 * One writing run: how it is going, what it was asked for, and once it has written one, its post
 * with the record of how it was written.
 */
export function WritingRunScreen({ projectId, runId }: WritingRunScreenProps) {
  const run = useWritingRun(projectId, runId);
  const finished = run.data !== undefined && isWritingRunFinished(run.data);
  const now = useNow(!finished);
  const item = useContentItem(projectId, run.data?.content_item_id ?? null);
  const project = useProject(projectId);
  const version = useProfileVersion(projectId, item.data?.profile_version ?? null);
  const proposals = useProposals(projectId);

  if (run.error !== null) {
    const missing = run.error.code === "writing_run_not_found";
    return (
      <div className="grid gap-8">
        <BackToWriting projectId={projectId} />
        {missing ? (
          <Notice tone="warning" title="No such writing run">
            <p>This project has no writing run with this ID. The address may name a run of another project.</p>
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
        <BackToWriting projectId={projectId} />
        <Skeleton className="h-48" />
      </div>
    );
  }

  const found = run.data;
  // The repository and the commit the profile version was analysed at; until both are read, or
  // when the repository is on no known host, the evidence is shown as paths without links.
  const source: EvidenceSource = {
    repositoryUrl: project.data?.repository_url ?? "",
    commitSha: version.data?.commit_sha ?? null,
  };

  return (
    <div className="grid gap-8">
      <BackToWriting projectId={projectId} />
      <WritingRunCard run={found} now={now} linked={false} />
      <RunBrief projectId={projectId} brief={runBrief(found)} />

      {found.content_item_id !== null && (
        <>
          {item.error !== null && (
            <RequestProblem title="The post could not be read" error={item.error} onRetry={() => void item.refetch()} />
          )}
          {item.data === undefined && item.error === null && <Skeleton className="h-64" />}
          {item.data !== undefined && (
            <PostRecord
              post={item.data}
              source={source}
              waitingOn={waitingPostProposal(proposals.data ?? [], found.id)}
            />
          )}
        </>
      )}
    </div>
  );
}

type PostRecordProps = {
  post: ContentItemDetail;
  source: EvidenceSource;
  waitingOn: ReturnType<typeof waitingPostProposal>;
};

/** The post and the record of how it was written, each read from the run's plain data once. */
function PostRecord({ post, source, waitingOn }: PostRecordProps) {
  return (
    <>
      <PostReview
        post={post}
        check={checkReportOf(post.check_report)}
        review={reviewOf(post.critic_report)}
        flaggedNumbers={post.flagged_numbers}
        waitingOn={waitingOn}
      />
      <PostClaims claims={claimsOf(post.claims)} source={source} />
      <PostAlternatives alternatives={alternativesOf(post.alternatives)} />
      <PostPlan plan={planOf(post.plan)} changes={post.plan_changes} />
    </>
  );
}

/** The way back to the project's writing, since a run's page hides the list. */
function BackToWriting({ projectId }: { projectId: string }) {
  return (
    <Link
      href={projectPageHref(projectId, "writing")}
      className="inline-flex w-fit items-center gap-1.5 rounded-sm text-sm text-muted-foreground hover:text-foreground"
    >
      <ArrowLeftIcon aria-hidden="true" className="size-3.5" />
      All writing
    </Link>
  );
}
