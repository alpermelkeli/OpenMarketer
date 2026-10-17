"use client";

/**
 * A chat's events, live: the history the API pages, newest first, and then everything after it as
 * the API's event stream sends it, both kept in one query's cache.
 *
 * The stream is an `EventSource` on the dashboard's proxy, opened once the first page of history is
 * in, from the newest event it holds. When the API ends the stream (it does after a few minutes) or
 * the connection drops, the browser reconnects by itself and sends the last id it saw, so nothing
 * is missed or repeated; an event that still arrives twice is counted once by its id. An event that
 * ends a turn or a research run refreshes what it changed (`refreshedByChatEvent`). Nothing is
 * polled: the stream says when something has changed.
 */

import { type InfiniteData, useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";

import { EVENT_KINDS, inOrder, newestEventId, parseChatEvent } from "@/lib/chat/events";

import type { ApiError } from "./api-error";
import { api, dataOf } from "./client";
import { nextCursor } from "./pages";
import { queryKeys, refreshedByChatEvent } from "./query-keys";
import type { ChatEvent, ChatEventList } from "./types";

/** A turn records a few events and a research run a few dozen, so a page holds several turns. */
const EVENT_PAGE_SIZE = 100;

type EventPages = InfiniteData<ChatEventList, string | undefined>;

export type ChatEvents = {
  /** The events so far, oldest first; undefined until the first page is in. */
  events: ChatEvent[] | undefined;
  error: ApiError | null;
  hasOlderPage: boolean;
  pagesLoaded: number;
  isFetchingOlderPage: boolean;
  fetchOlderPage: () => void;
  /** The stream was refused and the browser will not reconnect: what is shown stops moving. */
  streamClosed: boolean;
};

export function useChatEvents(projectId: string, chatId: string): ChatEvents {
  const queryClient = useQueryClient();
  const queryKey = queryKeys.chatEvents(projectId, chatId);
  const history = useInfiniteQuery<ChatEventList, ApiError, EventPages, typeof queryKey, string | undefined>({
    queryKey,
    queryFn: async ({ pageParam }) =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/chats/{chat_id}/events", {
          params: {
            path: { project_id: projectId, chat_id: chatId },
            query: { limit: EVENT_PAGE_SIZE, cursor: pageParam },
          },
        }),
      ),
    initialPageParam: undefined,
    getNextPageParam: nextCursor,
    // The stream keeps the pages current; reading them again would only repeat what it said.
    staleTime: Infinity,
    retry: false,
  });
  const historyLoaded = history.data !== undefined;
  const streamClosed = useEventStream(projectId, chatId, historyLoaded, queryClient);
  const events = useMemo(
    () => (history.data === undefined ? undefined : inOrder(history.data.pages.flatMap((page) => page.events))),
    [history.data],
  );

  const { fetchNextPage } = history;
  // Asked for again while a page is loading, it waits for that page instead of starting over.
  const fetchOlderPage = useCallback(() => void fetchNextPage({ cancelRefetch: false }), [fetchNextPage]);

  return {
    events,
    error: history.error,
    hasOlderPage: history.hasNextPage,
    pagesLoaded: history.data?.pages.length ?? 0,
    isFetchingOlderPage: history.isFetchingNextPage,
    fetchOlderPage,
    streamClosed,
  };
}

function useEventStream(
  projectId: string,
  chatId: string,
  historyLoaded: boolean,
  queryClient: ReturnType<typeof useQueryClient>,
): boolean {
  const [closedFor, setClosedFor] = useState<string | null>(null);
  useEffect(() => {
    if (!historyLoaded) return;
    const queryKey = queryKeys.chatEvents(projectId, chatId);
    const loaded = queryClient.getQueryData<EventPages>(queryKey)?.pages.flatMap((page) => page.events) ?? [];
    const source = new EventSource(streamUrl(projectId, chatId, newestEventId(loaded)));

    const receive = (message: MessageEvent<string>) => {
      const event = parseChatEvent(message.data);
      if (event === null) return;
      queryClient.setQueryData<EventPages>(queryKey, (data) => withEvent(data, event));
      for (const refresh of refreshedByChatEvent(projectId, chatId, event)) {
        void queryClient.invalidateQueries(refresh);
      }
    };
    // A dropped connection is retried by the browser; a refused one (not an event stream) is not.
    const closed = () => {
      if (source.readyState === EventSource.CLOSED) setClosedFor(chatId);
    };
    for (const kind of EVENT_KINDS) source.addEventListener(kind, receive);
    source.addEventListener("error", closed);
    return () => source.close();
  }, [historyLoaded, projectId, chatId, queryClient]);
  return closedFor === chatId;
}

/** The proxy's stream of the chat's events after `after`, from the chat's first event without it. */
function streamUrl(projectId: string, chatId: string, after: number | null): string {
  const path = `/api/v1/projects/${encodeURIComponent(projectId)}/chats/${encodeURIComponent(chatId)}/events/stream`;
  return after === null ? path : `${path}?after=${after}`;
}

/** The pages with `event` added at the top of the newest, unless a page already holds it. */
function withEvent(data: EventPages | undefined, event: ChatEvent): EventPages | undefined {
  if (data === undefined) return data;
  if (data.pages.some((page) => page.events.some((held) => held.id === event.id))) return data;
  const [newest, ...older] = data.pages;
  if (newest === undefined) return data;
  return { ...data, pages: [{ ...newest, events: [event, ...newest.events] }, ...older] };
}
