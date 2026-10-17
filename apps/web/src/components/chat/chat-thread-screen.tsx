"use client";

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { InlineCodeText } from "@/components/common/inline-code-text";
import { Notice } from "@/components/common/notice";
import { RequestProblem } from "@/components/common/request-problem";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useChat, useChatMessages, useMarkChatRead, useSendChatMessage, useSubmission } from "@/lib/api/chats";
import { errorMessage } from "@/lib/api/error-message";
import type { ChatMessage } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { Composer } from "./composer";
import { MessageMarkdown } from "./message-markdown";

/**
 * One chat with the orchestrator: its messages, oldest first, and where the next one is written. A
 * message being answered is shown as sent, and one that failed is kept here to be sent again, since
 * the API stores nothing of a failed turn. Opening the chat marks it read.
 */
export function ChatThreadScreen({ projectId, chatId }: { projectId: string; chatId: string }) {
  const chat = useChat(projectId, chatId);
  const messages = useChatMessages(projectId, chatId);
  const submission = useSubmission(projectId, chatId);
  const send = useSendChatMessage(projectId);
  const [draft, setDraft] = useState("");
  useMarkReadWhileOpen(projectId, chatId, chat.data?.unread === true);

  const end = useRef<HTMLDivElement>(null);
  const shown = (messages.data?.length ?? 0) + (submission === null ? 0 : 1);
  useEffect(() => end.current?.scrollIntoView({ block: "end" }), [shown, submission?.status]);

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
          messages.data.map((message) => <MessageBubble key={message.id} message={message} />)
        )}
        {submission && (
          <>
            <Bubble author="You" mine>
              <MessageMarkdown text={submission.message} />
            </Bubble>
            {submission.status === "pending" ? (
              <p role="status" className="text-sm text-muted-foreground">
                The orchestrator is reading the project&hellip;
              </p>
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
        <div ref={end} />
      </div>

      <div className="sticky bottom-0 bg-background pb-2">
        <Composer
          value={draft}
          onChange={setDraft}
          onSend={sendMessage}
          busy={submission?.status === "pending"}
          placeholder="Reply to the orchestrator…"
        />
      </div>
    </section>
  );
}

function MessageBubble({ message }: { message: ChatMessage }) {
  const mine = message.role === "user";
  return (
    <Bubble author={mine ? "You" : "Orchestrator"} mine={mine}>
      <MessageMarkdown text={message.content} />
    </Bubble>
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
 * Whenever the chat has a message the person has not read while it is open, record that they have.
 * After a refusal it stops: the chat stays unread, which is what the API says.
 */
function useMarkReadWhileOpen(projectId: string, chatId: string, unread: boolean) {
  const { mutate, status } = useMarkChatRead(projectId, chatId);
  useEffect(() => {
    if (unread && (status === "idle" || status === "success")) mutate();
  }, [unread, status, mutate]);
}
