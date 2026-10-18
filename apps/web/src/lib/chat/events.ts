/**
 * A chat's events, folded into what its screen shows: each turn of the orchestrator with the tools
 * it called, and each research run and studio run a turn started with its stages as they arrived.
 *
 * Events arrive from two places, the history the API pages and the live stream, and a retried step
 * of a run may record the same thing twice, so the fold is forgiving: an event is counted once by
 * its id, events are taken in the order of their ids whatever order they came in, a tool call that
 * repeats one still waiting for its answer is the same call, and a stage identical to the one
 * before it is shown once. A detail is read defensively, a value of the wrong type being taken as
 * missing, and text in it (a tool's label, a query, a page's title or address, a reason) is kept as
 * text for the screen to show as such, never as markup or as a link it has not checked. An id in
 * it (a studio run's version or asset) is kept as it came: the screen builds an image's address
 * from it only after checking it is one (`lib/studio/image-src.ts`).
 *
 * Display only: what a run did is the server's record, and nothing here decides anything.
 */

import type { ChatEvent, ChatMessage } from "@/lib/api/types";

export const CHAT_RUN = "orchestrator-chat";
export const RESEARCH_RUN = "research-run";
export const STUDIO_RUN = "studio-run";

/** Every kind of event the API records for a chat, which a stream listens for by name. */
export const EVENT_KINDS = [
  "turn_started",
  "tool_called",
  "tool_answered",
  "run_started",
  "turn_answered",
  "turn_failed",
  "planned",
  "searched",
  "screened",
  "fetched",
  "fetch_failed",
  "read",
  "synthesized",
  "briefed",
  "produced",
  "checked",
  "verified",
  "technique_switched",
  "edit_planned",
  "finalized",
  "escalated",
  "finished",
  "failed",
] as const;

/** The events after which what the API says of the chat, its messages or a run has changed. */
export const TURN_ENDS: readonly string[] = ["turn_answered", "turn_failed"];
export const RUN_ENDS: readonly string[] = ["finished", "failed"];

/** What a turn answers: a person's message, or the end of a run a turn of the chat started. */
export type TurnTrigger = "message" | "research_finished" | "studio_finished";

const FOLLOW_UP_TRIGGERS: readonly TurnTrigger[] = ["research_finished", "studio_finished"];

export type TurnStep = {
  tool: string;
  /** What the orchestrator said it was doing, as text. */
  label: string;
  /** Null while the tool has not answered. */
  ok: boolean | null;
};

export type TurnView = {
  id: string;
  trigger: TurnTrigger;
  status: "running" | "answered" | "failed";
  steps: TurnStep[];
  /** The assistant message that answered it. */
  messageId: string | null;
  reason: string | null;
  /** The research runs it started, in the order it started them. */
  researchRunIds: string[];
  /** The studio runs it started, in the order it started them. */
  studioRunIds: string[];
  /** Whether its `turn_started` event is among those folded, so none of its steps is missing. */
  started: boolean;
  /** The id of its first event: where it begins in the chat's order. */
  firstEventId: number;
  /** When its first event was stored. */
  startedAt: string;
};

export type ResearchStage =
  | { kind: "planned"; round: number | null; queries: string[] }
  | { kind: "searched"; query: string; results: number; ok: boolean }
  | { kind: "screened"; kept: number; dropped: number }
  | { kind: "fetched"; url: string; title: string }
  | { kind: "fetch_failed"; url: string; reason: string }
  | { kind: "read"; pages: number; ok: boolean }
  | { kind: "synthesized"; findings: number; round: number | null };

export type ResearchRunView = {
  id: string;
  /** The turn that started it, when its `run_started` event is among those folded. */
  turnId: string | null;
  stages: ResearchStage[];
  status: "running" | "finished" | "failed";
  /** How many checked findings it has: the last count it recorded, or its final one. */
  findings: number | null;
  reason: string | null;
  firstEventId: number;
  startedAt: string;
};

/**
 * What a studio run recorded, step by step. A version id is the image a step made or judged; a
 * technique is the studio's name for how the image is made, shown by `lib/chat/studio-progress.ts`.
 */
export type StudioStage =
  | { kind: "briefed"; technique: string }
  | {
      kind: "produced";
      operation: string;
      /** Null when the operation made no image. */
      versionId: string | null;
      technique: string;
      round: number | null;
      ok: boolean;
    }
  /** The code's checks of the image: size, format, weight, and whether its words read as asked. */
  | { kind: "checked"; versionId: string | null; passed: boolean; failures: string[] }
  /** The verifier model's review: the criteria it failed at a blocking severity. */
  | { kind: "verified"; versionId: string | null; passed: boolean; failed: string[] }
  | { kind: "technique_switched"; from: string; to: string; round: number | null }
  | { kind: "edit_planned"; round: number | null; steps: number }
  | { kind: "finalized"; versionId: string | null; round: number | null }
  /** The run stopped before a candidate passed; `reason` is the code's sentence of why. */
  | { kind: "escalated"; versionId: string | null; reason: string };

export type StudioRunView = {
  id: string;
  /** The turn that started it, when its `run_started` event is among those folded. */
  turnId: string | null;
  stages: StudioStage[];
  status: "running" | "finished" | "failed";
  /** The asset it ended with, from its `finished` event; its steps do not name it. */
  assetId: string | null;
  /** The version it ended with, from its `finished` event. */
  finalVersionId: string | null;
  /** The newest image one of its steps made. */
  latestVersionId: string | null;
  /** From its `finished` event: whether it stopped at its limits with a candidate that did not pass. */
  needsReview: boolean | null;
  reason: string | null;
  firstEventId: number;
  startedAt: string;
};

export type ChatActivity = {
  /** In the order they began. */
  turns: TurnView[];
  /** In the order they began. */
  runs: ResearchRunView[];
  /** In the order they began. */
  studioRuns: StudioRunView[];
  /** The latest turn that has neither answered nor failed, if any. */
  runningTurn: TurnView | null;
};

type Detail = ChatEvent["detail"];

type Followed = { research: Map<string, ResearchRunView>; studio: Map<string, StudioRunView> };

export function foldChatEvents(events: readonly ChatEvent[]): ChatActivity {
  const turns = new Map<string, TurnView>();
  const followed: Followed = { research: new Map(), studio: new Map() };
  for (const event of inOrder(events)) {
    if (event.run_kind === CHAT_RUN) foldTurnEvent(turnOf(turns, event), event, followed);
    else if (event.run_kind === RESEARCH_RUN) foldResearchEvent(runOf(followed.research, event.run_id, event), event);
    else if (event.run_kind === STUDIO_RUN) foldStudioEvent(studioRunOf(followed.studio, event.run_id, event), event);
  }
  const turnList = [...turns.values()];
  return {
    turns: turnList,
    runs: [...followed.research.values()].sort((a, b) => a.firstEventId - b.firstEventId),
    studioRuns: [...followed.studio.values()].sort((a, b) => a.firstEventId - b.firstEventId),
    runningTurn: turnList.findLast((turn) => turn.status === "running") ?? null,
  };
}

/** Whether a turn was started by the end of a run rather than by a person's message. */
export function isFollowUp(turn: Pick<TurnView, "trigger">): boolean {
  return FOLLOW_UP_TRIGGERS.includes(turn.trigger);
}

/** Each event once, by its id, oldest first. */
export function inOrder(events: readonly ChatEvent[]): ChatEvent[] {
  const byId = new Map<number, ChatEvent>();
  for (const event of events) if (!byId.has(event.id)) byId.set(event.id, event);
  return [...byId.values()].sort((a, b) => a.id - b.id);
}

/** The id of the newest event, where a stream continues from; null when there is none. */
export function newestEventId(events: readonly ChatEvent[]): number | null {
  return events.reduce<number | null>((newest, event) => (newest === null || event.id > newest ? event.id : newest), null);
}

/**
 * An event as the stream's `data` line holds it, or null when it is not one: a frame the dashboard
 * cannot read is skipped rather than shown half-way.
 */
export function parseChatEvent(data: string): ChatEvent | null {
  let value: unknown;
  try {
    value = JSON.parse(data);
  } catch {
    return null;
  }
  if (!isRecord(value)) return null;
  const { id, run_kind: runKind, run_id: runId, kind, detail, created_at: createdAt } = value;
  if (typeof id !== "number" || !Number.isInteger(id)) return null;
  if (typeof runKind !== "string" || typeof runId !== "string" || typeof kind !== "string") return null;
  if (typeof createdAt !== "string" || !isRecord(detail)) return null;
  return { id, run_kind: runKind, run_id: runId, kind, detail: detail as Detail, created_at: createdAt };
}

function turnOf(turns: Map<string, TurnView>, event: ChatEvent): TurnView {
  let turn = turns.get(event.run_id);
  if (turn === undefined) {
    turn = {
      id: event.run_id,
      trigger: "message",
      status: "running",
      steps: [],
      messageId: null,
      reason: null,
      researchRunIds: [],
      studioRunIds: [],
      started: false,
      firstEventId: event.id,
      startedAt: event.created_at,
    };
    turns.set(event.run_id, turn);
  }
  return turn;
}

function runOf(runs: Map<string, ResearchRunView>, runId: string, event: ChatEvent): ResearchRunView {
  let run = runs.get(runId);
  if (run === undefined) {
    run = {
      id: runId,
      turnId: null,
      stages: [],
      status: "running",
      findings: null,
      reason: null,
      firstEventId: event.id,
      startedAt: event.created_at,
    };
    runs.set(runId, run);
  }
  return run;
}

function studioRunOf(runs: Map<string, StudioRunView>, runId: string, event: ChatEvent): StudioRunView {
  let run = runs.get(runId);
  if (run === undefined) {
    run = {
      id: runId,
      turnId: null,
      stages: [],
      status: "running",
      assetId: null,
      finalVersionId: null,
      latestVersionId: null,
      needsReview: null,
      reason: null,
      firstEventId: event.id,
      startedAt: event.created_at,
    };
    runs.set(runId, run);
  }
  return run;
}

function foldTurnEvent(turn: TurnView, event: ChatEvent, followed: Followed): void {
  const { detail } = event;
  switch (event.kind) {
    case "turn_started":
      turn.started = true;
      turn.trigger = triggerOf(textOf(detail, "trigger"));
      return;
    case "tool_called": {
      const tool = textOf(detail, "tool");
      if (tool === null) return;
      const label = textOf(detail, "label") ?? tool;
      const repeated = turn.steps.some((step) => step.ok === null && step.tool === tool && step.label === label);
      if (!repeated) turn.steps.push({ tool, label, ok: null });
      return;
    }
    case "tool_answered": {
      const tool = textOf(detail, "tool");
      const waiting = turn.steps.find((step) => step.ok === null && step.tool === tool);
      if (waiting !== undefined) waiting.ok = flagOf(detail, "ok") ?? true;
      return;
    }
    case "run_started": {
      const runId = textOf(detail, "run_id");
      if (runId === null) return;
      const runKind = textOf(detail, "run_kind");
      if (runKind === RESEARCH_RUN) {
        if (!turn.researchRunIds.includes(runId)) turn.researchRunIds.push(runId);
        runOf(followed.research, runId, event).turnId ??= turn.id;
      } else if (runKind === STUDIO_RUN) {
        if (!turn.studioRunIds.includes(runId)) turn.studioRunIds.push(runId);
        studioRunOf(followed.studio, runId, event).turnId ??= turn.id;
      }
      return;
    }
    case "turn_answered":
      turn.status = "answered";
      turn.messageId = textOf(detail, "message_id");
      return;
    case "turn_failed":
      turn.status = "failed";
      turn.reason = textOf(detail, "reason");
      return;
  }
}

function foldResearchEvent(run: ResearchRunView, event: ChatEvent): void {
  const { detail } = event;
  if (event.kind === "finished") {
    run.status = "finished";
    run.findings = countOf(detail, "findings") ?? run.findings;
    return;
  }
  if (event.kind === "failed") {
    run.status = "failed";
    run.reason = textOf(detail, "reason");
    return;
  }
  const stage = stageOf(event.kind, detail);
  if (stage === null) return;
  if (stage.kind === "synthesized") run.findings = stage.findings;
  const previous = run.stages.at(-1);
  if (previous !== undefined && sameStage(previous, stage)) return;
  run.stages.push(stage);
}

function stageOf(kind: string, detail: Detail): ResearchStage | null {
  switch (kind) {
    case "planned":
      return { kind, round: countOf(detail, "round"), queries: textsOf(detail, "queries") };
    case "searched": {
      const query = textOf(detail, "query");
      if (query === null) return null;
      return { kind, query, results: countOf(detail, "results") ?? 0, ok: flagOf(detail, "ok") ?? true };
    }
    case "screened":
      return { kind, kept: countOf(detail, "kept") ?? 0, dropped: countOf(detail, "dropped") ?? 0 };
    case "fetched": {
      const url = textOf(detail, "url");
      if (url === null) return null;
      return { kind, url, title: textOf(detail, "title") ?? "" };
    }
    case "fetch_failed": {
      const url = textOf(detail, "url");
      if (url === null) return null;
      return { kind, url, reason: textOf(detail, "reason") ?? "" };
    }
    case "read":
      return { kind, pages: countOf(detail, "pages") ?? 0, ok: flagOf(detail, "ok") ?? true };
    case "synthesized":
      return { kind, findings: countOf(detail, "findings") ?? 0, round: countOf(detail, "round") };
    default:
      return null;
  }
}

function triggerOf(trigger: string | null): TurnTrigger {
  return FOLLOW_UP_TRIGGERS.find((known) => known === trigger) ?? "message";
}

function foldStudioEvent(run: StudioRunView, event: ChatEvent): void {
  const { detail } = event;
  if (event.kind === "finished") {
    run.status = "finished";
    run.assetId = textOf(detail, "asset_id");
    run.finalVersionId = textOf(detail, "version_id");
    run.needsReview = flagOf(detail, "needs_review");
    return;
  }
  if (event.kind === "failed") {
    run.status = "failed";
    run.reason = textOf(detail, "reason");
    return;
  }
  const stage = studioStageOf(event.kind, detail);
  if (stage === null) return;
  if (stage.kind === "produced" && stage.versionId !== null) run.latestVersionId = stage.versionId;
  const previous = run.stages.at(-1);
  if (previous !== undefined && sameStage(previous, stage)) return;
  run.stages.push(stage);
}

function studioStageOf(kind: string, detail: Detail): StudioStage | null {
  switch (kind) {
    case "briefed": {
      const technique = textOf(detail, "technique");
      return technique === null ? null : { kind, technique };
    }
    case "produced": {
      const operation = textOf(detail, "operation");
      if (operation === null) return null;
      const ok = flagOf(detail, "ok") ?? false;
      return {
        kind,
        operation,
        versionId: ok ? textOf(detail, "version_id") : null,
        technique: textOf(detail, "technique") ?? "",
        round: countOf(detail, "round"),
        ok,
      };
    }
    case "checked": {
      const failures = textsOf(detail, "failures");
      const passed = flagOf(detail, "passed") ?? failures.length === 0;
      return { kind, versionId: textOf(detail, "version_id"), passed, failures };
    }
    case "verified": {
      const failed = textsOf(detail, "failed");
      const passed = flagOf(detail, "passed") ?? failed.length === 0;
      return { kind, versionId: textOf(detail, "version_id"), passed, failed };
    }
    case "technique_switched": {
      const from = textOf(detail, "from_technique");
      const to = textOf(detail, "to_technique");
      if (from === null || to === null) return null;
      return { kind, from, to, round: countOf(detail, "round") };
    }
    case "edit_planned":
      return { kind, round: countOf(detail, "round"), steps: countOf(detail, "steps") ?? 0 };
    case "finalized":
      return { kind, versionId: textOf(detail, "version_id"), round: countOf(detail, "round") };
    case "escalated":
      return { kind, versionId: textOf(detail, "version_id"), reason: textOf(detail, "reason") ?? "" };
    default:
      return null;
  }
}

function sameStage<T>(a: T, b: T): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function textOf(detail: Detail, key: string): string | null {
  const value = detail[key];
  return typeof value === "string" ? value : null;
}

function countOf(detail: Detail, key: string): number | null {
  const value = detail[key];
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

function flagOf(detail: Detail, key: string): boolean | null {
  const value = detail[key];
  return typeof value === "boolean" ? value : null;
}

function textsOf(detail: Detail, key: string): string[] {
  const value = detail[key];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * The turn a message just sent is being answered by: the latest turn that answers a message, while
 * it runs and, once answered, until its answer is among the messages shown. None before it has
 * started, and none when the latest such turn failed: that was an earlier attempt.
 */
export function awaitedTurn(activity: ChatActivity, shown: readonly ChatMessage[]): TurnView | null {
  const latest = activity.turns.findLast((turn) => turn.trigger === "message");
  if (latest === undefined || latest.status === "failed") return null;
  if (latest.status === "running") return latest;
  return shown.some((message) => message.id === latest.messageId) ? null : latest;
}

/** The most pages of older events a screen loads to cover the messages it shows. */
export const MAX_HISTORY_PAGES = 10;

type History = {
  activity: ChatActivity;
  pagesLoaded: number;
  hasOlderPage: boolean;
  /** The turn of the oldest message shown that has one; null when none has. */
  oldestTurnId: string | null;
};

/**
 * Whether an older page of events is needed so that the oldest message shown has its turn's steps:
 * while that turn's start is not among the events loaded, up to `MAX_HISTORY_PAGES`. Pages come
 * newest first, so once the oldest turn's start is loaded, every later turn's events are too. A
 * turn's messages are stored when it ends, after its events, so a time cannot tell this.
 */
export function needsOlderEvents({ activity, pagesLoaded, hasOlderPage, oldestTurnId }: History): boolean {
  if (!hasOlderPage || oldestTurnId === null || pagesLoaded >= MAX_HISTORY_PAGES) return false;
  return activity.turns.find((turn) => turn.id === oldestTurnId)?.started !== true;
}
