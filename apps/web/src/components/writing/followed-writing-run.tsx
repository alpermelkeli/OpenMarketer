"use client";

import { useWritingRun } from "@/lib/api/writing";
import type { WritingRun } from "@/lib/api/types";
import { useNow } from "@/lib/use-now";
import { isWritingRunFinished } from "@/lib/writing/status";

import { WritingRunCard } from "./writing-run-card";

type FollowedWritingRunProps = {
  projectId: string;
  /** The run as the list gave it: unfinished, so its state is asked for until it ends. */
  run: WritingRun;
};

/** An unfinished writing run, followed on the server and shown with a running clock. */
export function FollowedWritingRun({ projectId, run: listed }: FollowedWritingRunProps) {
  const { data } = useWritingRun(projectId, listed.id, listed);
  const run = data ?? listed;
  const now = useNow(!isWritingRunFinished(run));
  return <WritingRunCard run={run} now={now} linked />;
}
