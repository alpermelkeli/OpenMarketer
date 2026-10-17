"use client";

/**
 * Query and mutation hooks for a project's chats with the orchestrator: the list, one chat, its
 * messages, and sending one. A turn is answered while the request is open, and a turn that fails
 * stores nothing, so sending the same message again is the way to retry it.
 */

import { useInfiniteQuery, useMutation, useMutationState, useQuery, useQueryClient } from "@tanstack/react-query";

import type { ApiError } from "./api-error";
import { api, dataOf, doneOf } from "./client";
import { PAGE_SIZE, mergePages, nextCursor } from "./pages";
import { queryKeys } from "./query-keys";
import type { Chat, ChatList, ChatMessage, ChatMessageList, ChatTurn } from "./types";

async function chatPage(projectId: string, cursor: string | undefined): Promise<ChatList> {
  return dataOf(
    await api.GET("/v1/projects/{project_id}/chats", {
      params: { path: { project_id: projectId }, query: { limit: PAGE_SIZE, cursor } },
    }),
  );
}

/**
 * The newest page of the project's chats, most recently active first: what the sidebar lists. Under a
 * key of its own (see `queryKeys.shellProject`).
 */
export function useShellChats(projectId: string) {
  return useQuery<ChatList, ApiError, Chat[]>({
    queryKey: queryKeys.shellChats(projectId),
    queryFn: () => chatPage(projectId, undefined),
    select: (page) => page.chats,
  });
}

export function useChat(projectId: string, chatId: string) {
  return useQuery<Chat, ApiError>({
    queryKey: queryKeys.chat(projectId, chatId),
    queryFn: async () =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/chats/{chat_id}", {
          params: { path: { project_id: projectId, chat_id: chatId } },
        }),
      ),
    retry: false,
  });
}

/** A chat's messages, oldest first as a conversation reads; the API pages them newest first. */
export function useChatMessages(projectId: string, chatId: string) {
  return useInfiniteQuery<
    ChatMessageList,
    ApiError,
    ChatMessage[],
    ReturnType<typeof queryKeys.chatMessages>,
    string | undefined
  >({
    queryKey: queryKeys.chatMessages(projectId, chatId),
    queryFn: async ({ pageParam }) =>
      dataOf(
        await api.GET("/v1/projects/{project_id}/chats/{chat_id}/messages", {
          params: {
            path: { project_id: projectId, chat_id: chatId },
            query: { limit: PAGE_SIZE, cursor: pageParam },
          },
        }),
      ),
    initialPageParam: undefined,
    getNextPageParam: nextCursor,
    select: (data) =>
      mergePages(
        data.pages.map((page) => page.messages),
        (message) => message.id,
      ).reverse(),
    retry: false,
  });
}

/** Open an empty chat; the API names it after its first message. */
export function useCreateChat(projectId: string) {
  return useMutation<Chat, ApiError, void>({
    mutationFn: async () =>
      dataOf(
        await api.POST("/v1/projects/{project_id}/chats", {
          params: { path: { project_id: projectId } },
          body: {},
        }),
      ),
  });
}

export type ChatSubmission = { chatId: string; message: string };

const sendKey = (projectId: string) => ["send-chat-message", projectId] as const;

/**
 * Send one message and wait for the orchestrator's answer. The mutation outlives the screen that
 * started it: a new chat sends its first message and moves to the chat, which shows the message as
 * sent (`useSubmission`) until the stored exchange has been read back.
 */
export function useSendChatMessage(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<ChatTurn, ApiError, ChatSubmission>({
    mutationKey: sendKey(projectId),
    mutationFn: async ({ chatId, message }) =>
      dataOf(
        await api.POST("/v1/projects/{project_id}/chats/{chat_id}/messages", {
          params: { path: { project_id: projectId, chat_id: chatId } },
          body: { message },
        }),
      ),
    // Settled only once the exchange is read back, so the sent message never disappears in between.
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.chatScope(projectId) }),
  });
}

export type Submission = {
  message: string;
  status: "pending" | "error";
  error: ApiError | null;
};

/**
 * The chat's latest message that is still being answered or that failed, from this browser tab; null
 * when the latest one was answered. A failed message is stored nowhere, so this is the only place
 * it is kept.
 */
export function useSubmission(projectId: string, chatId: string): Submission | null {
  const submissions = useMutationState({
    filters: { mutationKey: sendKey(projectId) },
    select: (mutation) => ({
      variables: mutation.state.variables as ChatSubmission | undefined,
      status: mutation.state.status,
      error: mutation.state.error as ApiError | null,
    }),
  });
  const latest = submissions.filter((submission) => submission.variables?.chatId === chatId).at(-1);
  if (latest?.variables === undefined || (latest.status !== "pending" && latest.status !== "error")) return null;
  return {
    message: latest.variables.message,
    status: latest.status,
    error: latest.error,
  };
}

/**
 * Delete a chat for good, with its messages. The chat and the messages it held are
 * dropped at once; the list and the sidebar are read again for the rest.
 */
export function useDeleteChat(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: async (chatId) =>
      doneOf(
        await api.DELETE("/v1/projects/{project_id}/chats/{chat_id}", {
          params: { path: { project_id: projectId, chat_id: chatId } },
        }),
      ),
    onSuccess: (_deleted, chatId) => {
      queryClient.removeQueries({ queryKey: queryKeys.chat(projectId, chatId) });
      queryClient.removeQueries({ queryKey: queryKeys.chatMessages(projectId, chatId) });
      return queryClient.invalidateQueries({ queryKey: queryKeys.chatScope(projectId) });
    },
  });
}

/** Record that the person has read the chat up to now. */
export function useMarkChatRead(projectId: string, chatId: string) {
  const queryClient = useQueryClient();
  return useMutation<Chat, ApiError, void>({
    mutationFn: async () =>
      dataOf(
        await api.POST("/v1/projects/{project_id}/chats/{chat_id}/read", {
          params: { path: { project_id: projectId, chat_id: chatId } },
        }),
      ),
    onSuccess: (chat) => {
      queryClient.setQueryData(queryKeys.chat(projectId, chatId), chat);
      return queryClient.invalidateQueries({ queryKey: queryKeys.chats(projectId) });
    },
  });
}
