"use client";

import { useStudioRun } from "@/lib/api/studio";
import type { StudioRunView } from "@/lib/chat/events";
import { candidateAlt, latestCandidate, studioProgressStatus } from "@/lib/chat/studio-progress";

import { StudioProgressCard } from "./studio-progress-card";

type FollowedStudioProgressProps = { projectId: string; view: StudioRunView };

/**
 * A studio run of the chat, from its events and its own record. The record is read once and not
 * polled: the event that ends the run refreshes it. It settles a run that ended without an event,
 * such as one whose workflow could not be started, and gives the platform the image is for.
 */
export function FollowedStudioProgress({ projectId, view }: FollowedStudioProgressProps) {
  const run = useStudioRun(projectId, view.id, undefined, { poll: false });
  const status = studioProgressStatus(view, run.data);
  return (
    <StudioProgressCard
      projectId={projectId}
      view={view}
      status={status}
      candidate={latestCandidate(view, status)}
      alt={candidateAlt(run.data?.platform)}
    />
  );
}
