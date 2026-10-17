"use client";

import { useQueryClient } from "@tanstack/react-query";
import { BellIcon } from "lucide-react";
import Link from "next/link";
import { useCallback } from "react";

import { Spinner } from "@/components/common/spinner";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTitle, PopoverTrigger } from "@/components/ui/popover";
import { useProjects } from "@/lib/api/projects";
import { useWaitingProposals } from "@/lib/api/proposals";
import { queryKeys, refreshedByWorkspaceEvent } from "@/lib/api/query-keys";
import type { RunEvent } from "@/lib/api/types";
import { useWorkspaceEvents } from "@/lib/api/workspace-events";
import { shortId } from "@/lib/format";
import { effectLabel } from "@/lib/proposals/proposal-display";

/**
 * What waits for a person, told wherever they are: a bell at the top right with how many proposals
 * are waiting, and the list of them behind it. Each one links to its project's approvals screen,
 * where the decision is made.
 *
 * The list is read once and then refreshed the moment the workspace's event stream says a proposal
 * was stored or decided; nothing is polled. The workspace stream starts at the newest event, so
 * after every (re)connect the list is read again: a proposal stored in between would be in neither.
 */
export function NotificationBell() {
  const queryClient = useQueryClient();
  const waiting = useWaitingProposals();
  const projects = useProjects();

  const refresh = useCallback(
    (event: RunEvent) => {
      for (const change of refreshedByWorkspaceEvent(event)) {
        void queryClient.invalidateQueries(change);
      }
    },
    [queryClient],
  );
  const catchUp = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: queryKeys.waitingProposals() });
  }, [queryClient]);
  useWorkspaceEvents(refresh, catchUp);

  const count = waiting.data?.length ?? 0;
  const names = new Map((projects.data ?? []).map((project) => [project.id, project.name]));

  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button
            variant="outline"
            size="icon"
            className="relative"
            aria-label={
              count === 0 ? "Notifications" : `Notifications: ${count} waiting for you`
            }
          >
            <BellIcon aria-hidden="true" />
            {count > 0 && (
              <span
                aria-hidden="true"
                className="absolute -top-1.5 -right-1.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-primary px-1 text-2xs font-medium text-primary-foreground"
              >
                {count}
              </span>
            )}
          </Button>
        }
      />
      <PopoverContent align="end" sideOffset={8} className="grid gap-3 p-4">
        <PopoverTitle>Waiting for you</PopoverTitle>
        {waiting.error !== null && (
          <p className="text-sm text-pretty text-muted-foreground">
            The waiting proposals could not be read.{" "}
            <button
              type="button"
              className="underline underline-offset-3 hover:text-foreground"
              onClick={() => void waiting.refetch()}
            >
              Try again
            </button>
          </p>
        )}
        {waiting.data === undefined && waiting.error === null && <Spinner className="size-4" />}
        {waiting.data?.length === 0 && (
          <p className="text-sm text-pretty text-muted-foreground">
            Nothing waits for you. An agent asks here before it publishes or spends, and its run
            waits until you decide.
          </p>
        )}
        {waiting.data !== undefined && waiting.data.length > 0 && (
          <ul className="grid gap-2">
            {waiting.data.map((proposal) => (
              <li key={proposal.id}>
                <Link
                  href={`/projects/${encodeURIComponent(proposal.project_id)}/approvals`}
                  className="block rounded-md border bg-card p-3 transition-colors hover:bg-muted"
                >
                  <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="font-mono text-sm">{proposal.action_name}</span>
                    <span className="text-2xs tracking-widest text-muted-foreground uppercase">
                      {effectLabel(proposal.effect)}
                    </span>
                  </span>
                  <span className="mt-1 block text-xs text-muted-foreground">
                    {names.get(proposal.project_id) ?? shortId(proposal.project_id)} is waiting for
                    your decision
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </PopoverContent>
    </Popover>
  );
}
