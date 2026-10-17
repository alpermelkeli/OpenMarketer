"use client";

/**
 * The workspace's events, live: what every run of every project records, as the API's workspace
 * stream sends it.
 *
 * One `EventSource` on the dashboard's proxy, opened where the app is framed, so it lives on every
 * screen. Without a cursor the stream starts at the newest event, so the client follows what
 * happens from now on; when the API ends the stream (it does after a few minutes) the browser
 * reconnects by itself and sends the last id it saw, so nothing is missed, and a `503` while too
 * many streams are open is retried like a dropped connection. Nothing is polled.
 *
 * The two callbacks must keep their identity across renders (wrap them in `useCallback`): a new
 * one reconnects the stream. `onOpen` runs after every (re)connect, where a reader that also
 * loads the state the events change should catch up on what it may have missed.
 */

import { useEffect } from "react";

import { EVENT_KINDS, parseRunEvent } from "@/lib/chat/events";

import type { RunEvent } from "./types";

export function useWorkspaceEvents(onEvent: (event: RunEvent) => void, onOpen: () => void): void {
  useEffect(() => {
    const source = new EventSource("/api/v1/events/stream");
    const receive = (message: MessageEvent<string>) => {
      const event = parseRunEvent(message.data);
      if (event !== null) onEvent(event);
    };
    for (const kind of EVENT_KINDS) source.addEventListener(kind, receive);
    source.addEventListener("open", onOpen);
    return () => source.close();
  }, [onEvent, onOpen]);
}
