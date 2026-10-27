"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";

import { LoadMore } from "@/components/common/load-more";
import { Notice } from "@/components/common/notice";
import { RequestProblem } from "@/components/common/request-problem";
import { Skeleton } from "@/components/ui/skeleton";
import { useProfileVersion } from "@/lib/api/profiles";
import { useProject } from "@/lib/api/projects";
import { useContentItems, useStartWritingRun, useWritingRuns } from "@/lib/api/writing";
import { projectPageHref, writingHref } from "@/lib/projects/project-navigation";
import { announceableFeatures, briefProblems } from "@/lib/writing/brief-form";
import { isWritingRunFinished } from "@/lib/writing/status";

import { FollowedWritingRun } from "./followed-writing-run";
import { NewPostForm } from "./new-post-form";
import { PostList } from "./post-list";
import { WritingRunCard } from "./writing-run-card";

type WritingScreenProps = { projectId: string };

/**
 * The project's writing: the posts its runs wrote, each with what became of it, the runs
 * themselves, and a brief to start one.
 */
export function WritingScreen({ projectId }: WritingScreenProps) {
  const router = useRouter();
  const project = useProject(projectId);
  const approvedVersion = project.data?.latest_approved_version ?? null;
  const approved = useProfileVersion(projectId, approvedVersion);
  const runs = useWritingRuns(projectId);
  const posts = useContentItems(projectId);
  const start = useStartWritingRun(projectId);

  const noApprovedProfile = project.data !== undefined && approvedVersion === null;
  const features = noApprovedProfile
    ? []
    : approved.data === undefined
      ? null
      : announceableFeatures(approved.data.profile);

  return (
    <div className="grid gap-8">
      <section aria-labelledby="about-writing" className="max-w-prose">
        <h2 id="about-writing" className="font-heading text-title">
          Write posts for the product
        </h2>
        <p className="mt-2 text-pretty text-muted-foreground">
          A writing run writes one post for X from the approved profile. It plans the post, writes a few texts, checks
          each in code (its length as X counts it, no links, no @mentions, nothing the profile says never to mention,
          every claim resting on a live feature), has a second model read them as a critic, and revises until one
          passes. The best one waits in{" "}
          <Link
            href={projectPageHref(projectId, "approvals")}
            className="rounded-sm text-brand underline-offset-4 hover:underline"
          >
            Approvals
          </Link>{" "}
          for you. Nothing is sent to X yet: an approved post is recorded as a dry run.
        </p>
      </section>

      <section aria-labelledby="posts" className="grid gap-4 border-t pt-8">
        <h2 id="posts" className="font-heading text-lg">
          Posts
        </h2>
        {posts.error !== null && (
          <RequestProblem
            title="The posts could not be read"
            error={posts.error}
            onRetry={() => void posts.refetch()}
          />
        )}
        {posts.data === undefined && posts.error === null && <Skeleton className="h-28" />}
        {posts.data?.length === 0 && (
          <p className="max-w-prose text-sm text-pretty text-muted-foreground">
            No post has been written for this project yet. Start a run below.
          </p>
        )}
        {posts.data !== undefined && posts.data.length > 0 && <PostList posts={posts.data} />}
        <LoadMore
          noun="posts"
          hasMore={posts.hasNextPage}
          loading={posts.isFetchingNextPage}
          onLoadMore={() => void posts.fetchNextPage()}
        />
      </section>

      <section aria-labelledby="runs" className="grid gap-4 border-t pt-8">
        <h2 id="runs" className="font-heading text-lg">
          Runs
        </h2>
        {runs.error !== null && (
          <RequestProblem title="The runs could not be read" error={runs.error} onRetry={() => void runs.refetch()} />
        )}
        {runs.data === undefined && runs.error === null && <Skeleton className="h-28" />}
        {runs.data?.length === 0 && (
          <p className="max-w-prose text-sm text-pretty text-muted-foreground">
            No writing run has been started for this project yet.
          </p>
        )}
        {runs.data?.map((run) =>
          isWritingRunFinished(run) ? (
            <WritingRunCard key={run.id} run={run} now={0} linked />
          ) : (
            <FollowedWritingRun key={run.id} projectId={projectId} run={run} />
          ),
        )}
        <LoadMore
          noun="runs"
          hasMore={runs.hasNextPage}
          loading={runs.isFetchingNextPage}
          onLoadMore={() => void runs.fetchNextPage()}
        />
      </section>

      <section aria-labelledby="new-post" className="grid gap-4 border-t pt-8">
        <h2 id="new-post" className="font-heading text-lg">
          New post
        </h2>
        {noApprovedProfile && (
          <Notice tone="warning" title="The profile is not approved yet" className="max-w-prose">
            The writer writes only from an approved profile, so a run started now would fail. Review and approve{" "}
            <Link
              href={projectPageHref(projectId, "profile")}
              className="rounded-sm text-brand underline-offset-4 hover:underline"
            >
              the profile
            </Link>{" "}
            first.
          </Notice>
        )}
        {approved.error !== null && (
          <RequestProblem
            title="The approved profile could not be read, so its live features cannot be offered"
            error={approved.error}
            onRetry={() => void approved.refetch()}
          />
        )}
        <NewPostForm
          features={features}
          problems={briefProblems(start.error)}
          pending={start.isPending}
          onSubmit={(brief) =>
            start.mutate(brief, {
              onSuccess: (run) => router.push(writingHref(projectId, run.id)),
            })
          }
        />
      </section>
    </div>
  );
}
