/** Query keys of the server state the dashboard holds, in one place so invalidation cannot drift. */

import {
  CHAT_RUN,
  PROPOSAL_KINDS,
  RESEARCH_RUN,
  RUN_ENDS,
  STUDIO_RUN,
  TURN_ENDS,
} from "@/lib/chat/events";

import type { RunEvent } from "./types";

export const queryKeys = {
  projects: () => ["projects", "list"] as const,
  /** Everything held about one project: the project itself, its runs, its profile versions and its proposals. */
  projectScope: (projectId: string) => ["projects", "one", projectId] as const,
  project: (projectId: string) =>
    ["projects", "one", projectId, "project"] as const,
  analyses: (projectId: string) =>
    ["projects", "one", projectId, "analyses", "list"] as const,
  analysisRun: (projectId: string, runId: string) =>
    ["projects", "one", projectId, "analyses", "run", runId] as const,
  researches: (projectId: string) =>
    ["projects", "one", projectId, "researches", "list"] as const,
  researchRun: (projectId: string, runId: string) =>
    ["projects", "one", projectId, "researches", "run", runId] as const,
  researchReport: (projectId: string, runId: string) =>
    [
      "projects",
      "one",
      projectId,
      "researches",
      "run",
      runId,
      "report",
    ] as const,
  studioRuns: (projectId: string) =>
    ["projects", "one", projectId, "studio-runs", "list"] as const,
  studioRun: (projectId: string, runId: string) =>
    ["projects", "one", projectId, "studio-runs", "run", runId] as const,
  /** The project's library: the assets its studio runs made. */
  mediaAssets: (projectId: string) =>
    ["projects", "one", projectId, "media-assets", "list"] as const,
  /** One asset with its versions. */
  mediaAsset: (projectId: string, assetId: string) =>
    ["projects", "one", projectId, "media-assets", "one", assetId] as const,
  /**
   * The project's repository images: what the last analysis found and what a person added. Under
   * the project's scope, so an analysis that ends refreshes them with everything else it changed
   * (`useAnalysisRun`).
   */
  repositoryImages: (projectId: string) =>
    ["projects", "one", projectId, "repository-images", "list"] as const,
  profileVersions: (projectId: string) =>
    ["projects", "one", projectId, "versions", "list"] as const,
  profileVersion: (projectId: string, version: number) =>
    ["projects", "one", projectId, "versions", "one", version] as const,
  proposals: (projectId: string) =>
    ["projects", "one", projectId, "proposals", "list"] as const,
  /** Every proposal of the workspace that waits for a person, wherever it is: the notifications. */
  waitingProposals: () => ["proposals", "waiting"] as const,
  /** Everything held about a project's chats: the list, each chat and its messages. */
  chatScope: (projectId: string) =>
    ["projects", "one", projectId, "chats"] as const,
  chats: (projectId: string) =>
    ["projects", "one", projectId, "chats", "list"] as const,
  chat: (projectId: string, chatId: string) =>
    ["projects", "one", projectId, "chats", "one", chatId] as const,
  chatMessages: (projectId: string, chatId: string) =>
    ["projects", "one", projectId, "chats", "one", chatId, "messages"] as const,
  /** The chat's events: the pages of its history, with what its stream has added since. */
  chatEvents: (projectId: string, chatId: string) =>
    ["projects", "one", projectId, "chats", "one", chatId, "events"] as const,
  /**
   * The copies that the parts around a screen read: the sidebar and the project's frame. They hydrate
   * before the screen inside them, so if they shared a screen's key, the screen would hydrate from data
   * already fetched and not match the server's HTML. Under the keys of what they show, they are
   * refreshed whenever the project or its proposals are.
   */
  shellProject: (projectId: string) =>
    ["projects", "one", projectId, "project", "shell"] as const,
  shellWaitingProposals: (projectId: string) =>
    ["projects", "one", projectId, "proposals", "list", "shell"] as const,
  shellChats: (projectId: string) =>
    ["projects", "one", projectId, "chats", "list", "shell"] as const,
  /** Where each credential's value comes from. Never a value: no response holds one. */
  credentials: () => ["settings", "credentials"] as const,
  /** Everything about the models: the settings, and the check that depends on them. */
  modelScope: () => ["settings", "models"] as const,
  modelSettings: () => ["settings", "models", "settings"] as const,
  modelCheck: () => ["settings", "models", "check"] as const,
  /** The provider's catalogue: it does not change with the workspace's choices. */
  modelCatalog: () => ["settings", "model-catalog"] as const,
  analysisSettings: () => ["settings", "analysis"] as const,
  /** The caps of what generated media may cost, with this month's spending. */
  creativeBudget: () => ["settings", "creative-budget"] as const,
};

type Refresh = { queryKey: readonly unknown[]; exact?: boolean };

/**
 * What a studio run that has just ended makes out of date: the list of runs, the library, which
 * now holds the asset it made, and this month's spending, which its images count towards.
 */
export function refreshedByStudioRunEnd(projectId: string): Refresh[] {
  return [
    { queryKey: queryKeys.studioRuns(projectId) },
    { queryKey: queryKeys.mediaAssets(projectId) },
    { queryKey: queryKeys.creativeBudget() },
  ];
}

/** What a decision on an asset, or another of its versions chosen, makes out of date: the library that lists its state. The asset itself is set from the answer. */
export function refreshedByAssetDecision(projectId: string): Refresh[] {
  return [{ queryKey: queryKeys.mediaAssets(projectId) }];
}

/**
 * What adding, relabelling or removing a repository image makes out of date: the list of them,
 * whose groups follow each image's kind. Nothing else holds one.
 */
export function refreshedByRepositoryImageChange(projectId: string): Refresh[] {
  return [{ queryKey: queryKeys.repositoryImages(projectId) }];
}

/**
 * What deleting a studio run makes out of date: the runs and the library. The run and its asset
 * are dropped, not read again: they are gone.
 */
export function refreshedByStudioRunDeleted(projectId: string): Refresh[] {
  return [
    { queryKey: queryKeys.studioRuns(projectId) },
    { queryKey: queryKeys.mediaAssets(projectId) },
  ];
}

/**
 * What a chat's event makes out of date. A turn that starts or ends changes whether the chat is
 * busy, and one that ends has stored its messages; a run a turn starts is in the project's list of
 * its kind. A research run that ends has its final state and perhaps a report; a studio run that
 * ends has its final state, an asset in the library and its cost in this month's spending. Either
 * may start a follow-up turn, which makes the chat busy. A proposal event of either kind of run
 * changes the project's proposals, which the approvals screen and the sidebar's count read. The
 * chat's own key is refreshed alone (`exact`), so its events, which the stream keeps current, are
 * not read again.
 */
export function refreshedByChatEvent(
  projectId: string,
  chatId: string,
  event: RunEvent,
): Refresh[] {
  const chat = { queryKey: queryKeys.chat(projectId, chatId), exact: true };
  if (event.run_kind === CHAT_RUN) {
    if (event.kind === "turn_started") return [chat];
    if (TURN_ENDS.includes(event.kind)) {
      return [
        chat,
        { queryKey: queryKeys.chatMessages(projectId, chatId) },
        { queryKey: queryKeys.chats(projectId) },
      ];
    }
    if (event.kind === "run_started")
      return refreshedByRunStarted(projectId, event.detail.run_kind);
    return [];
  }
  if (event.run_kind !== RESEARCH_RUN && event.run_kind !== STUDIO_RUN)
    return [];
  if (PROPOSAL_KINDS.includes(event.kind))
    return [{ queryKey: queryKeys.proposals(projectId) }];
  if (!RUN_ENDS.includes(event.kind)) return [];
  if (event.run_kind === RESEARCH_RUN) {
    return [
      { queryKey: queryKeys.researchRun(projectId, event.run_id) },
      { queryKey: queryKeys.researches(projectId) },
      chat,
    ];
  }
  return [
    { queryKey: queryKeys.studioRun(projectId, event.run_id) },
    ...refreshedByStudioRunEnd(projectId),
    chat,
  ];
}

function refreshedByRunStarted(projectId: string, runKind: unknown): Refresh[] {
  if (runKind === RESEARCH_RUN)
    return [{ queryKey: queryKeys.researches(projectId) }];
  if (runKind === STUDIO_RUN)
    return [{ queryKey: queryKeys.studioRuns(projectId) }];
  return [];
}

/**
 * What a workspace event makes out of date: whether an action waits for a person changes the
 * workspace's list of what waits, which is what the notifications show. A project's own screens
 * are kept current by its chat streams' events (`refreshedByChatEvent`).
 */
export function refreshedByWorkspaceEvent(event: RunEvent): Refresh[] {
  if (PROPOSAL_KINDS.includes(event.kind)) {
    return [{ queryKey: queryKeys.waitingProposals() }];
  }
  return [];
}
