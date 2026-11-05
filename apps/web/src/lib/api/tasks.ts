"use client";

/**
 * The query hook for one task of the orchestrator: its request, its plan and the plans its
 * changes replaced, the steps it dispatched with their runs, and its report. A task is started by
 * the chat, never here, and nothing here confirms, changes or cancels one: its plan is confirmed
 * on its proposal (`proposals.ts`).
 */

import { useQuery } from "@tanstack/react-query";

import { isTaskFinished } from "@/lib/chat/task-progress";

import type { ApiError } from "./api-error";
import { api, dataOf } from "./client";
import { queryKeys } from "./query-keys";
import type { TaskDetail } from "./types";

const POLL_INTERVAL_MS = 5_000;

/**
 * One task as it is now. Its events refresh it (`refreshedByChatEvent`), but a step's start
 * records none, so a screen that shows its steps polls it, with `poll`, until it has ended. A
 * screen that only needs what does not change (its chat) reads it once.
 */
export function useTask(projectId: string, taskId: string, { poll = false }: { poll?: boolean } = {}) {
  return useQuery<TaskDetail, ApiError>({
    queryKey: queryKeys.task(projectId, taskId),
    queryFn: async () =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/tasks/{task_id}", {
          params: { path: { project_id: projectId, task_id: taskId } },
        }),
      ),
    refetchInterval: (query) => {
      if (!poll) return false;
      const task = query.state.data;
      return task !== undefined && isTaskFinished(task) ? false : POLL_INTERVAL_MS;
    },
    staleTime: 0,
    retry: false,
  });
}
