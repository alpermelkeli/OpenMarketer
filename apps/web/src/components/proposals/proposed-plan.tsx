"use client";

import { ArrowRightIcon } from "lucide-react";
import Link from "next/link";

import { Notice } from "@/components/common/notice";
import { TaskPlan } from "@/components/tasks/task-plan";
import { useTask } from "@/lib/api/tasks";
import type { Proposal } from "@/lib/api/types";
import { isChatId } from "@/lib/projects/project-id";
import { chatHref } from "@/lib/projects/project-navigation";
import { proposedPlanOf } from "@/lib/tasks/plan";

/**
 * The plan a task's `confirm_plan` asks a person to confirm, read from its payload: what the
 * person confirms is in the proposal itself. With it, the way to the chat that started the task,
 * where the task's card shows how it goes. A payload that is no plan is said, never guessed at.
 */
export function ProposedPlan({ proposal }: { proposal: Proposal }) {
  const plan = proposedPlanOf(proposal);
  return (
    <div className="grid gap-2">
      <p className="text-2xs tracking-widest text-muted-foreground uppercase">The plan to confirm</p>
      {plan === null ? (
        <Notice tone="warning" title="The plan cannot be shown">
          The proposal does not hold a plan the dashboard can read. Read the request below before you decide.
        </Notice>
      ) : (
        <div className="max-w-prose rounded-sm border border-dashed border-warning/60 p-3 text-sm">
          <TaskPlan plan={plan} />
        </div>
      )}
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
        <span>Confirmed, the task runs these steps; rejected, it ends without running any of them.</span>
        <TaskChatLink projectId={proposal.project_id} taskId={proposal.run_id} />
      </p>
    </div>
  );
}

function TaskChatLink({ projectId, taskId }: { projectId: string; taskId: string }) {
  const task = useTask(projectId, taskId);
  const chatId = task.data?.chat_thread_id ?? null;
  if (task.data === undefined) return null;
  if (chatId === null || !isChatId(chatId)) return <span>The chat that started it was deleted.</span>;
  return (
    <Link
      href={chatHref(projectId, encodeURIComponent(chatId))}
      className="inline-flex items-center gap-1 rounded-sm text-brand underline-offset-4 hover:underline"
    >
      Its chat
      <ArrowRightIcon aria-hidden="true" className="size-3.5" />
    </Link>
  );
}
