"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";

import { Notice } from "@/components/common/notice";
import { RequestProblem } from "@/components/common/request-problem";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useProfileVersion, useProfileVersions } from "@/lib/api/profiles";
import { useProject } from "@/lib/api/projects";
import { versionFromAddress, versionToOpen } from "@/lib/profile/version-labels";

import { LatestVersions } from "./latest-versions";
import { ProfileReview } from "./profile-review";
import { VersionHistory } from "./version-history";

type ProfileReviewScreenProps = { projectId: string };

/**
 * The profile screen: which version is on show (the one in the address, or the one
 * that most needs a reader), the ways to reach the others, and the review of it.
 */
export function ProfileReviewScreen({ projectId }: ProfileReviewScreenProps) {
  const requested = versionFromAddress(useSearchParams().get("version"));
  const project = useProject(projectId);
  const versions = useProfileVersions(projectId);
  const [editing, setEditing] = useState(false);

  const shownNumber = requested ?? (project.data === undefined ? null : versionToOpen(project.data));
  const version = useProfileVersion(projectId, shownNumber);
  const parent = useProfileVersion(projectId, version.data?.edited_from_version ?? null);

  // The frame around this screen reports a project that cannot be read.
  if (project.data === undefined) return project.error === null ? <Loading /> : null;

  if (shownNumber === null) {
    return (
      <div className="grid animate-enter justify-items-start gap-4 py-6">
        <div>
          <h2 className="font-heading text-title">No profile yet</h2>
          <p className="mt-2 max-w-prose text-pretty text-muted-foreground">
            This project has no profile version. An analysis reads the repository and drafts one for you to review.
          </p>
        </div>
        <Button size="lg" nativeButton={false} render={<Link href={`/projects/${projectId}/analyses`} />}>
          Go to analyses
        </Button>
      </div>
    );
  }

  const latest = [project.data.latest_draft_version, project.data.latest_approved_version];

  return (
    <div className="grid gap-8">
      <div className="grid gap-3">
        <LatestVersions
          projectId={projectId}
          latestDraft={project.data.latest_draft_version}
          latestApproved={project.data.latest_approved_version}
          versions={versions.data ?? []}
          shownVersion={shownNumber}
          locked={editing}
        />
        {versions.error !== null && (
          <RequestProblem
            title="The version history could not be read"
            error={versions.error}
            onRetry={() => void versions.refetch()}
          />
        )}
        {versions.data !== undefined && (
          <VersionHistory
            projectId={projectId}
            versions={versions.data}
            shownVersion={shownNumber}
            defaultOpen={!latest.includes(shownNumber)}
            locked={editing}
            hasMore={versions.hasNextPage}
            loadingMore={versions.isFetchingNextPage}
            onLoadMore={() => void versions.fetchNextPage()}
          />
        )}
      </div>

      {version.error?.code === "profile_not_found" && (
        <Notice tone="warning" title={`This project has no version ${shownNumber}`}>
          <Link
            href={`/projects/${projectId}/profile`}
            className="rounded-sm text-brand underline-offset-4 hover:underline"
          >
            Open the current version
          </Link>
        </Notice>
      )}
      {version.error !== null && version.error.code !== "profile_not_found" && (
        <RequestProblem
          title={`Version ${shownNumber} could not be read`}
          error={version.error}
          onRetry={() => void version.refetch()}
        />
      )}
      {version.data === undefined && version.error === null && <Loading />}

      {version.data !== undefined && (
        <ProfileReview
          key={version.data.version}
          project={project.data}
          version={version.data}
          parent={parent.data ?? null}
          onEditingChange={setEditing}
        />
      )}
    </div>
  );
}

function Loading() {
  return (
    <div className="grid gap-4" aria-busy="true" aria-label="Loading the profile">
      <Skeleton className="h-20" />
      <Skeleton className="h-72" />
    </div>
  );
}
