"use client";

import { Notice } from "@/components/common/notice";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage } from "@/lib/api/error-message";
import { useApprovedProfile, useDraftProfile } from "@/lib/api/profiles";
import { findProject } from "@/lib/remembered/remembered";
import { useRemembered } from "@/lib/remembered/use-remembered";

import { ProfileEmptyState } from "./profile-empty-state";
import { ProfileReview } from "./profile-review";

type ProfileReviewScreenProps = { projectId: string };

/** Reads the project's latest draft and latest approved profile and hands them to the review. */
export function ProfileReviewScreen({ projectId }: ProfileReviewScreenProps) {
  const draft = useDraftProfile(projectId);
  const approved = useApprovedProfile(projectId);
  const { remembered } = useRemembered();

  const error = draft.error ?? approved.error;
  if (error !== null) {
    return (
      <Notice tone="danger" title="The profile could not be read" role="alert">
        <p>{errorMessage(error)}</p>
        <Button
          variant="outline"
          size="sm"
          className="mt-3"
          onClick={() => {
            void draft.refetch();
            void approved.refetch();
          }}
        >
          Try again
        </Button>
      </Notice>
    );
  }

  if (draft.data === undefined || approved.data === undefined) {
    return (
      <div className="grid gap-4" aria-busy="true" aria-label="Loading the profile">
        <Skeleton className="h-20" />
        <Skeleton className="h-16" />
        <Skeleton className="h-72" />
      </div>
    );
  }

  if (draft.data === null && approved.data === null) {
    return <ProfileEmptyState projectId={projectId} />;
  }

  const project = remembered === null ? null : findProject(remembered, projectId);
  return (
    <ProfileReview
      projectId={projectId}
      repositoryUrl={project?.repository_url ?? null}
      draft={draft.data}
      approved={approved.data}
    />
  );
}
