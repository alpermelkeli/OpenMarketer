/**
 * A chat's events, folded into what its screen shows: each turn of the orchestrator with the tools
 * it called, and each research run a turn started with its stages as they arrived.
 *
 * Events arrive from two places, the history the API pages and the live stream, and a retried step
 * of a run may record the same thing twice, so the fold is forgiving: an event is counted once by
 * its id, events are taken in the order of their ids whatever order they came in, a tool call that
 * repeats one still waiting for its answer is the same call, and a research stage identical to the
 * one before it is shown once. A detail is read defensively, a value of the wrong type being taken
 * as missing, and text in it (a tool's label, a query, a page's title or address) is kept as text
 * for the screen to show as such, never as markup or as a link it has not checked.
 *
 * Display only: what a run did is the server's record, and nothing here decides anything.
 */

import type { ChatMessage, RunEvent } from "@/lib/api/types";

export const CHAT_RUN = "orchestrator-chat";
export const RESEARCH_RUN = "research-run";

/** Every kind of run event the API records, which a stream listens for by name. */
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
  "finished",
  "failed",
  "proposal_waiting",
  "proposal_decided",
  "proposal_expired",
] as const;

/** The kinds that say where an action the policy gate held for a person stands. */
export const PROPOSAL_KINDS: readonly string[] = [
  "proposal_waiting",
  "proposal_decided",
  "proposal_expired",
];

/** The events after which what the API says of the chat, its messages or a run has changed. */
export const TURN_ENDS: readonly string[] = ["turn_answered", "turn_failed"];
export const RESEARCH_ENDS: readonly string[] = ["finished", "failed"];

export type TurnTrigger = "message" | "research_finished";

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
  /** The action the policy gate held for a person, and where it stands. */
  proposal: ResearchProposal | null;
  firstEventId: number;
  startedAt: string;
};

export type ResearchProposal = {
  id: string;
  actionName: string;
  /** What the action does beyond the run, as the event worded it ("publishes", "spends"). */
  effect: string;
  /** "waiting" until the person decides or the deadline passes. */
  status: "waiting" | "approved" | "rejected" | "expired";
};

export type ChatActivity = {
  /** In the order they began. */
  turns: TurnView[];
  /** In the order they began. */
  runs: ResearchRunView[];
  /** The latest turn that has neither answered nor failed, if any. */
  runningTurn: TurnView | null;
};

type Detail = RunEvent["detail"];

export function foldChatEvents(events: readonly RunEvent[]): ChatActivity {
  const turns = new Map<string, TurnView>();
  const runs = new Map<string, ResearchRunView>();
  for (const event of inOrder(events)) {
    if (event.run_kind === CHAT_RUN) foldTurnEvent(turnOf(turns, event), event, runs);
    else if (event.run_kind === RESEARCH_RUN) foldResearchEvent(runOf(runs, event.run_id, event), event);
  }
  const turnList = [...turns.values()];
  return {
    turns: turnList,
    runs: [...runs.values()].sort((a, b) => a.firstEventId - b.firstEventId),
    runningTurn: turnList.findLast((turn) => turn.status === "running") ?? null,
  };
}

/** Each event once, by its id, oldest first. */
export function inOrder(events: readonly RunEvent[]): RunEvent[] {
  const byId = new Map<number, RunEvent>();
  for (const event of events) if (!byId.has(event.id)) byId.set(event.id, event);
  return [...byId.values()].sort((a, b) => a.id - b.id);
}

/** The id of the newest event, where a stream continues from; null when there is none. */
export function newestEventId(events: readonly RunEvent[]): number | null {
  return events.reduce<number | null>((newest, event) => (newest === null || event.id > newest ? event.id : newest), null);
}

/**
 * An event as the stream's `data` line holds it, or null when it is not one: a frame the dashboard
 * cannot read is skipped rather than shown half-way.
 */
export function parseRunEvent(data: string): RunEvent | null {
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

function turnOf(turns: Map<string, TurnView>, event: RunEvent): TurnView {
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
      started: false,
      firstEventId: event.id,
      startedAt: event.created_at,
    };
    turns.set(event.run_id, turn);
  }
  return turn;
}

function runOf(runs: Map<string, ResearchRunView>, runId: string, event: RunEvent): ResearchRunView {
  let run = runs.get(runId);
  if (run === undefined) {
    run = {
      id: runId,
      turnId: null,
      stages: [],
      status: "running",
      findings: null,
      reason: null,
      proposal: null,
      firstEventId: event.id,
      startedAt: event.created_at,
    };
    runs.set(runId, run);
  }
  return run;
}

function foldTurnEvent(turn: TurnView, event: RunEvent, runs: Map<string, ResearchRunView>): void {
  const { detail } = event;
  switch (event.kind) {
    case "turn_started":
      turn.started = true;
      turn.trigger = textOf(detail, "trigger") === "research_finished" ? "research_finished" : "message";
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
      if (textOf(detail, "run_kind") !== RESEARCH_RUN || runId === null) return;
      if (!turn.researchRunIds.includes(runId)) turn.researchRunIds.push(runId);
      const run = runOf(runs, runId, event);
      run.turnId ??= turn.id;
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

function foldResearchEvent(run: ResearchRunView, event: RunEvent): void {
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
  if (PROPOSAL_KINDS.includes(event.kind)) {
    foldProposalEvent(run, event);
    return;
  }
  const stage = stageOf(event.kind, detail);
  if (stage === null) return;
  if (stage.kind === "synthesized") run.findings = stage.findings;
  const previous = run.stages.at(-1);
  if (previous !== undefined && sameStage(previous, stage)) return;
  run.stages.push(stage);
}

function foldProposalEvent(run: ResearchRunView, event: RunEvent): void {
  const { detail } = event;
  if (event.kind === "proposal_waiting") {
    const id = textOf(detail, "proposal_id");
    if (id === null) return;
    run.proposal = {
      id,
      actionName: textOf(detail, "action_name") ?? "action",
      effect: textOf(detail, "effect") ?? "",
      status: "waiting",
    };
    return;
  }
  // A decision or an expiry of another proposal (the harmless repeat of a retried step) changes nothing.
  if (run.proposal === null || run.proposal.id !== textOf(detail, "proposal_id")) return;
  if (event.kind === "proposal_expired") {
    run.proposal.status = "expired";
    return;
  }
  run.proposal.status = flagOf(detail, "approved") === false ? "rejected" : "approved";
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

function sameStage(a: ResearchStage, b: ResearchStage): boolean {
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
