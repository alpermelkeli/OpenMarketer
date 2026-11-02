"use client";

import { useMemo } from "react";

import { useWaitingProposals } from "@/lib/api/proposals";
import { useTask } from "@/lib/api/tasks";
import type { TaskRunView } from "@/lib/chat/events";
import { taskProgressStatus, waitingPostsOf } from "@/lib/chat/task-progress";
import { proposedPlanOf } from "@/lib/tasks/plan";

import { TaskProgressCard } from "./task-progress-card";

type FollowedTaskProgressProps = { projectId: string; view: TaskRunView };

/**
 * A task of the chat, from its events and its own record. Its events refresh the record, and it is
 * polled until it has ended too, because a step's start records no event. The plan a waiting
 * confirmation asks about, and the posts of its writing steps that wait for a person, are read
 * from what waits in the workspace, which the notifications keep current.
 */
export function FollowedTaskProgress({ projectId, view }: FollowedTaskProgressProps) {
  const task = useTask(projectId, view.id, { poll: true });
  const waiting = useWaitingProposals();
  const pending = waiting.data;
  const confirmation = view.proposal?.status === "waiting" ? view.proposal.id : null;
  const planToConfirm = useMemo(() => {
    const proposal = pending?.find((item) => item.id === confirmation);
    return proposal === undefined ? null : proposedPlanOf(proposal);
  }, [pending, confirmation]);
  const waitingPosts = useMemo(() => waitingPostsOf(pending ?? [], task.data?.steps ?? []), [pending, task.data]);

  return (
    <TaskProgressCard
      projectId={projectId}
      view={view}
      status={taskProgressStatus(view, task.data)}
      task={task.data}
      planToConfirm={planToConfirm}
      waitingPosts={waitingPosts}
    />
  );
}
