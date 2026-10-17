"use client";

import { SendIcon } from "lucide-react";
import Link from "next/link";
import { useId, useState } from "react";

import { Notice } from "@/components/common/notice";
import { StatusPill } from "@/components/common/status-pill";
import { Button, buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useProject } from "@/lib/api/projects";
import { useProposals } from "@/lib/api/proposals";
import type { Project } from "@/lib/api/types";
import { draftAwaitingReview, projectStates } from "@/lib/projects/project-summary";
import { projectPageHref } from "@/lib/projects/project-navigation";

/** Requests a person may start from; each only fills the message, which the person can change. */
const SUGGESTIONS = [
  "Write a post announcing our newest feature",
  "What did our competitors post last month?",
  "Make a poster for the next release",
  "How did last week's posts do?",
];

/**
 * A new chat with the project's orchestrator: where a project opens. The orchestrator is designed
 * (the design report's console section) and not built, so this screen shows where a request will be
 * written and sends nothing.
 */
export function NewChatScreen({ projectId }: { projectId: string }) {
  const loaded = useProject(projectId).data;
  const proposals = useProposals(projectId);
  const waiting = proposals.data?.filter((proposal) => proposal.status === "pending").length ?? 0;
  const [message, setMessage] = useState("");
  const messageId = useId();

  return (
    <section aria-labelledby="new-chat" className="grid min-h-[60dvh] grid-cols-[minmax(0,1fr)] content-center gap-8 py-6">
      <div className="grid justify-items-center gap-3 text-center">
        <h1 id="new-chat" className="font-heading text-display text-balance">
          {loaded ? `What shall we do for ${loaded.name}?` : <Skeleton className="h-10 w-full max-w-96" />}
        </h1>
        {loaded && (
          <ul aria-label="Where the project stands" className="flex flex-wrap justify-center gap-1.5">
            {projectStates(loaded).map((state) => (
              <li key={state.label}>
                <StatusPill tone={state.tone} live={state.tone === "progress"}>
                  {state.label}
                </StatusPill>
              </li>
            ))}
            {waiting > 0 && (
              <li>
                <StatusPill tone="warning">
                  {waiting === 1 ? "1 proposal waits for you" : `${waiting} proposals wait for you`}
                </StatusPill>
              </li>
            )}
          </ul>
        )}
      </div>

      {loaded && loaded.latest_approved_version === null && <BeforeTheFirstChat project={loaded} />}

      <form className="mx-auto grid w-full max-w-prose gap-2" onSubmit={(event) => event.preventDefault()}>
        <label htmlFor={messageId} className="sr-only">
          Message to the orchestrator
        </label>
        <div className="flex items-end gap-2 rounded-md border bg-card p-2 shadow-sm">
          <Textarea
            id={messageId}
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            placeholder="Ask about the project, or ask for something to be done…"
            className="min-h-20 min-w-0 resize-none border-0 shadow-none focus-visible:ring-0"
          />
          <Button type="submit" size="icon" disabled aria-describedby={`${messageId}-not-built`}>
            <SendIcon aria-hidden="true" />
            <span className="sr-only">Send</span>
          </Button>
        </div>
        <p id={`${messageId}-not-built`} className="text-xs text-muted-foreground">
          The orchestrator that will answer here is not built yet, so nothing can be sent.
        </p>
      </form>

      <ul aria-label="Requests to start from" className="mx-auto grid w-full max-w-prose gap-2 sm:grid-cols-2">
        {SUGGESTIONS.map((suggestion) => (
          <li key={suggestion}>
            <button
              type="button"
              onClick={() => setMessage(suggestion)}
              className="h-full w-full rounded-md border bg-card px-3 py-2.5 text-left text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              {suggestion}
            </button>
          </li>
        ))}
      </ul>

      <p className="mx-auto max-w-prose text-center text-xs text-pretty text-muted-foreground">
        The orchestrator will start the project&rsquo;s agents and follow their work in the chat. Nothing is published
        until you approve it.
      </p>
    </section>
  );
}

/** Before a profile is approved, the orchestrator has nothing to work from: the way to one. */
function BeforeTheFirstChat({ project }: { project: Project }) {
  const next = nextStepToAProfile(project);
  return (
    <Notice tone="warning" className="mx-auto w-full max-w-prose">
      <div className="flex flex-wrap items-center gap-3">
        <p className="mr-auto">
          {next.text} The orchestrator works from the approved Product Profile, so approve one first.
        </p>
        <Link
          href={projectPageHref(project.id, next.page)}
          className={buttonVariants({ variant: "outline", size: "sm" })}
        >
          {next.action}
        </Link>
      </div>
    </Notice>
  );
}

function nextStepToAProfile(project: Project): {
  text: string;
  page: "profile" | "analyses";
  action: string;
} {
  const draft = draftAwaitingReview(project);
  if (draft !== null) {
    return {
      text: `Draft version ${draft} of the profile waits for your review.`,
      page: "profile",
      action: "Review the profile",
    };
  }
  if (project.unfinished_run_id !== null) {
    return {
      text: "An analysis of the repository is running.",
      page: "analyses",
      action: "Follow the analysis",
    };
  }
  return {
    text: "The repository has not been analysed yet.",
    page: "analyses",
    action: "Start an analysis",
  };
}
