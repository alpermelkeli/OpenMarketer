"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { InlineCodeText } from "@/components/common/inline-code-text";
import { Notice } from "@/components/common/notice";
import { StatusPill } from "@/components/common/status-pill";
import { buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useCreateChat, useSendChatMessage } from "@/lib/api/chats";
import { errorMessage } from "@/lib/api/error-message";
import { useProject } from "@/lib/api/projects";
import { useProposals } from "@/lib/api/proposals";
import type { Project } from "@/lib/api/types";
import { chatHref, projectPageHref } from "@/lib/projects/project-navigation";
import { draftAwaitingReview, projectStates } from "@/lib/projects/project-summary";

import { Composer } from "./composer";

/**
 * Questions a person may start from, all of which the orchestrator can answer from the stored records;
 * each only fills the message, which the person can change.
 */
const SUGGESTIONS = [
  "What does the product do, according to its profile?",
  "Which features are live, and what is the evidence?",
  "How did the latest analysis go?",
  "What waits for my approval?",
];

/**
 * A new chat with the project's orchestrator: where a project opens. Sending the first message opens
 * the chat, which the API names after it, and moves to the chat while the message is answered.
 */
export function NewChatScreen({ projectId }: { projectId: string }) {
  const loaded = useProject(projectId).data;
  const proposals = useProposals(projectId);
  const waiting = proposals.data?.filter((proposal) => proposal.status === "pending").length ?? 0;
  const [message, setMessage] = useState("");
  const router = useRouter();
  const createChat = useCreateChat(projectId);
  const send = useSendChatMessage(projectId);

  async function start(first: string) {
    const chat = await createChat.mutateAsync().catch(() => null);
    if (chat === null) return;
    send.mutate({ chatId: chat.id, message: first });
    // The screen is kept while hidden, so it is emptied for the next new chat.
    setMessage("");
    router.push(chatHref(projectId, chat.id));
  }

  return (
    <section
      aria-labelledby="new-chat"
      className="grid min-h-[60dvh] grid-cols-[minmax(0,1fr)] content-center gap-8 py-6"
    >
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

      <div className="mx-auto grid w-full max-w-prose gap-2">
        <Composer
          value={message}
          onChange={setMessage}
          onSend={start}
          busy={createChat.isPending}
          placeholder="Ask about the project, or ask for something to be done…"
        />
        {createChat.error && (
          <Notice tone="danger" title="The chat could not be opened" role="alert">
            <InlineCodeText text={errorMessage(createChat.error)} />
          </Notice>
        )}
      </div>

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
        The orchestrator answers from the project&rsquo;s stored records, its profile, analyses and proposals, and can
        start a research run. Nothing is published until you approve it.
      </p>
    </section>
  );
}

/** Before a profile is approved, the orchestrator has no settled account of the product: the way to one. */
function BeforeTheFirstChat({ project }: { project: Project }) {
  const next = nextStepToAProfile(project);
  return (
    <Notice tone="warning" className="mx-auto w-full max-w-prose">
      <div className="flex flex-wrap items-center gap-3">
        <p className="mr-auto">
          {next.text} Until a Product Profile is approved, the orchestrator answers from the latest draft, if there is
          one.
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
