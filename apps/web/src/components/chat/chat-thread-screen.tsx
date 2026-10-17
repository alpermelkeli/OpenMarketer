"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { InlineCodeText } from "@/components/common/inline-code-text";
import { Notice } from "@/components/common/notice";
import { RequestProblem } from "@/components/common/request-problem";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { type ChatEvents, useChatEvents } from "@/lib/api/chat-events";
import { useChat, useChatMessages, useMarkChatRead, useSendChatMessage, useSubmission } from "@/lib/api/chats";
import { errorMessage } from "@/lib/api/error-message";
import type { ChatMessage } from "@/lib/api/types";
import { type ChatActivity, type TurnView, awaitedTurn, foldChatEvents, needsOlderEvents } from "@/lib/chat/events";
import { chatTimeline } from "@/lib/chat/timeline";
import { cn } from "@/lib/utils";

import { Composer } from "./composer";
import { FollowedResearchProgress } from "./followed-research-progress";
import { MessageMarkdown } from "./message-markdown";
import { TurnActivity, TurnSteps } from "./turn-activity";

const REVIEWING = "The orchestrator is reviewing the research…";
const ANSWERING = "The orchestrator is answering another message of this chat…";

/**
 * One chat with the orchestrator: its messages, oldest first, the research runs its turns started,
 * and where the next one is written. A message being answered is shown as sent, with the steps of
 * its turn as they happen, and one that failed is kept here to be sent again, since the API stores
 * nothing of a failed turn. While the orchestrator answers a turn it started itself (when research
 * it asked for has ended), nothing can be sent. Opening the chat marks it read.
 */
export function ChatThreadScreen({ projectId, chatId }: { projectId: string; chatId: string }) {
  const chat = useChat(projectId, chatId);
  const messages = useChatMessages(projectId, chatId);
  const events = useChatEvents(projectId, chatId);
  const submission = useSubmission(projectId, chatId);
  const send = useSendChatMessage(projectId);
  const [draft, setDraft] = useState("");
  useMarkReadWhileOpen(projectId, chatId, chat.data?.unread === true);

  const activity = useMemo(() => foldChatEvents(events.events ?? []), [events.events]);
  useEventsCoveringMessages(events, activity, messages.data);
  const timeline = useMemo(
    () => chatTimeline(messages.data ?? [], activity.runs, activity.turns),
    [messages.data, activity],
  );
  const turnById = useMemo(() => new Map(activity.turns.map((turn) => [turn.id, turn])), [activity]);

  const pending = submission?.status === "pending";
  const running = activity.runningTurn;
  const busy = chat.data?.busy === true;
  const ownTurn = pending ? awaitedTurn(activity, messages.data ?? []) : null;
  // A turn running without this tab waiting for it: a follow-up, or one another tab started.
  const otherTurn = !pending && busy && running !== null ? running : null;
  const waiting = otherTurn === null ? undefined : otherTurn.trigger === "research_finished" ? REVIEWING : ANSWERING;
  const liveTurn = ownTurn ?? otherTurn;
  // Runs of a turn that has ended are in the timeline.
  const liveRuns =
    liveTurn?.status === "running" ? activity.runs.filter((run) => run.turnId === liveTurn.id) : [];

  const end = useRef<HTMLDivElement>(null);
  const shown = timeline.length + (submission === null ? 0 : 1) + (otherTurn === null ? 0 : 1);
  useEffect(
    () => end.current?.scrollIntoView({ block: "end" }),
    [shown, submission?.status, liveTurn?.steps.length],
  );

  if (chat.error) {
    return <RequestProblem title="This chat could not be read" error={chat.error} onRetry={() => chat.refetch()} />;
  }

  function sendMessage(message: string) {
    send.mutate({ chatId, message });
    setDraft("");
  }

  return (
    <section aria-labelledby="chat-title" className="grid min-h-[calc(100dvh-6rem)] grid-rows-[auto_1fr_auto] gap-4">
      <header className="border-b pb-3">
        <h1 id="chat-title" className="truncate font-heading text-title">
          {chat.data ? chat.data.title : <Skeleton className="h-7 w-64" />}
        </h1>
      </header>

      <div aria-live="polite" className="grid content-start gap-4">
        {messages.hasNextPage && (
          <div className="justify-self-center">
            <Button
              variant="outline"
              size="sm"
              onClick={() => messages.fetchNextPage()}
              disabled={messages.isFetchingNextPage}
            >
              {messages.isFetchingNextPage ? "Loading…" : "Load earlier messages"}
            </Button>
          </div>
        )}
        {messages.error ? (
          <RequestProblem
            title="The messages could not be read"
            error={messages.error}
            onRetry={() => messages.refetch()}
          />
        ) : messages.data === undefined ? (
          <Skeleton className="h-24" />
        ) : (
          timeline.map((item) => {
            if (item.kind === "message") {
              const turn = item.message.turn_id === null ? undefined : turnById.get(item.message.turn_id);
              return <MessageBubble key={item.message.id} message={item.message} turn={turn} />;
            }
            if (item.kind === "research") {
              return <FollowedResearchProgress key={item.run.id} projectId={projectId} view={item.run} />;
            }
            return <FailedFollowUp key={item.turn.id} turn={item.turn} />;
          })
        )}
        {submission && (
          <>
            <Bubble author="You" mine>
              <MessageMarkdown text={submission.message} />
            </Bubble>
            {submission.status === "pending" ? (
              <TurnActivity heading="The orchestrator is reading the project…" steps={ownTurn?.steps ?? []} />
            ) : submission.error?.code === "chat_busy" ? (
              <Notice title="The orchestrator was still busy with this chat, so the message was not sent" role="status">
                <p>It answers one turn of a chat at a time. Send the message again once it has finished.</p>
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-3"
                  disabled={busy}
                  onClick={() => sendMessage(submission.message)}
                >
                  Send again
                </Button>
              </Notice>
            ) : (
              <Notice tone="danger" title="The message was not answered, and nothing of it was stored" role="alert">
                {submission.error && (
                  <p>
                    <InlineCodeText text={errorMessage(submission.error)} />
                  </p>
                )}
                <Button variant="outline" size="sm" className="mt-3" onClick={() => sendMessage(submission.message)}>
                  Send again
                </Button>
              </Notice>
            )}
          </>
        )}
        {otherTurn !== null && <TurnActivity heading={waiting ?? ANSWERING} steps={otherTurn.steps} />}
        {liveRuns.map((run) => (
          <FollowedResearchProgress key={run.id} projectId={projectId} view={run} />
        ))}
        {events.error && (
          <p className="text-xs text-muted-foreground">
            The steps of the orchestrator&rsquo;s turns could not be read; the messages are all here.
          </p>
        )}
        {events.streamClosed && (
          <p className="text-xs text-muted-foreground">
            This page has stopped following the chat live. Reload it to see what has happened since.
          </p>
        )}
        <div ref={end} />
      </div>

      <div className="sticky bottom-0 bg-background pb-2">
        <Composer
          value={draft}
          onChange={setDraft}
          onSend={sendMessage}
          busy={pending}
          waiting={waiting}
          placeholder="Reply to the orchestrator…"
        />
      </div>
    </section>
  );
}

function MessageBubble({ message, turn }: { message: ChatMessage; turn: TurnView | undefined }) {
  const mine = message.role === "user";
  return (
    <Bubble author={mine ? "You" : "Orchestrator"} mine={mine}>
      <MessageMarkdown text={message.content} />
      {!mine && turn !== undefined && <TurnSteps steps={turn.steps} />}
    </Bubble>
  );
}

/** A turn the orchestrator started when research ended, which stored no answer. */
function FailedFollowUp({ turn }: { turn: TurnView }) {
  return (
    <Notice tone="danger" title="The orchestrator could not review the research" role="status" className="max-w-prose">
      <p>{turn.reason ?? "No reason was recorded."} The report is still on the run&rsquo;s page.</p>
    </Notice>
  );
}

function Bubble({ author, mine, children }: { author: string; mine: boolean; children: ReactNode }) {
  return (
    <article
      className={cn(
        "max-w-[min(42rem,85%)] rounded-lg border px-3.5 py-2.5 text-sm",
        mine ? "justify-self-end bg-brand/8" : "justify-self-start bg-card",
      )}
    >
      <p className="mb-1 text-xs text-muted-foreground">{author}</p>
      {children}
    </article>
  );
}

/**
 * Load older pages of events until the oldest message shown has its turn's steps, a page at a time.
 */
function useEventsCoveringMessages(
  events: ChatEvents,
  activity: ChatActivity,
  messages: readonly ChatMessage[] | undefined,
) {
  const oldestTurnId = messages?.find((message) => message.turn_id !== null)?.turn_id ?? null;
  const needed =
    !events.isFetchingOlderPage &&
    needsOlderEvents({ activity, pagesLoaded: events.pagesLoaded, hasOlderPage: events.hasOlderPage, oldestTurnId });
  const { fetchOlderPage } = events;
  useEffect(() => {
    if (needed) fetchOlderPage();
  }, [needed, fetchOlderPage]);
}

/**
 * Whenever the chat has a message the person has not read while it is open, record that they have.
 * After a refusal it stops: the chat stays unread, which is what the API says.
 */
function useMarkReadWhileOpen(projectId: string, chatId: string, unread: boolean) {
  const { mutate, status } = useMarkChatRead(projectId, chatId);
  useEffect(() => {
    if (unread && (status === "idle" || status === "success")) mutate();
  }, [unread, status, mutate]);
}
