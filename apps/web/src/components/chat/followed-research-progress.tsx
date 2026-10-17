"use client";

import { useResearchRun } from "@/lib/api/research";
import type { ResearchRunView } from "@/lib/chat/events";
import { progressStatus } from "@/lib/chat/research-progress";

import { ResearchProgressCard } from "./research-progress-card";

type FollowedResearchProgressProps = { projectId: string; view: ResearchRunView };

/**
 * A research run of the chat, from its events and its own record. The record is read once and not
 * polled: the event that ends the run refreshes it. It settles a run that ended without an event,
 * such as one whose workflow could not be started.
 */
export function FollowedResearchProgress({ projectId, view }: FollowedResearchProgressProps) {
  const run = useResearchRun(projectId, view.id, undefined, { poll: false });
  return <ResearchProgressCard projectId={projectId} view={view} status={progressStatus(view, run.data)} />;
}
