/**
 * The order a chat thread is read in: its stored messages, oldest first, with each research run
 * and studio run a turn started placed where it belongs among them.
 *
 * A run goes right after the answer of the turn that started it, which says it was started. When
 * that answer is not among the messages shown (the turn is still running or failed, or its
 * `run_started` event was not loaded), the run goes where its first event falls in time: before the
 * first message stored after it, or at the end. Runs placed at the same point keep the order they
 * began in, whatever their kind. A follow-up turn's answer is an ordinary message, stored after the
 * run ended, so it falls after the run's card by itself; a follow-up turn that failed stores no
 * message, so its failure is placed where it began in time, like a run without its answer. The
 * order depends only on what it is given.
 *
 * What is being answered right now (a message just sent, a follow-up turn) is not stored yet, and
 * the screen shows it after the timeline, with the runs that turn has started so far: those are
 * left out here.
 */

import type { ChatMessage } from "@/lib/api/types";

import { type ResearchRunView, type StudioRunView, type TurnView, isFollowUp } from "./events";

export type TimelineItem =
  | { kind: "message"; message: ChatMessage }
  | { kind: "research"; run: ResearchRunView }
  | { kind: "studio"; run: StudioRunView }
  | { kind: "failed-follow-up"; turn: TurnView };

type RunItem = Extract<TimelineItem, { kind: "research" | "studio" }>;
type Timed = Exclude<TimelineItem, { kind: "message" }>;

export function chatTimeline(
  messages: readonly ChatMessage[],
  runs: readonly ResearchRunView[],
  turns: readonly TurnView[],
  studioRuns: readonly StudioRunView[] = [],
): TimelineItem[] {
  const answerOfTurn = new Map(turns.map((turn) => [turn.id, turn.messageId]));
  const running = new Set(turns.filter((turn) => turn.status === "running").map((turn) => turn.id));
  const shown = new Set(messages.map((message) => message.id));
  const afterMessage = new Map<string, RunItem[]>();
  const byTime: { item: Timed; at: string; order: number }[] = [];

  const followed: RunItem[] = [
    ...runs.map((run): RunItem => ({ kind: "research", run })),
    ...studioRuns.map((run): RunItem => ({ kind: "studio", run })),
  ];
  for (const item of followed) {
    const { run } = item;
    if (run.turnId !== null && running.has(run.turnId)) continue;
    const answer = run.turnId === null ? null : (answerOfTurn.get(run.turnId) ?? null);
    if (answer !== null && shown.has(answer)) append(afterMessage, answer, item);
    else byTime.push({ item, at: run.startedAt, order: run.firstEventId });
  }
  for (const turn of turns) {
    if (isFollowUp(turn) && turn.status === "failed") {
      byTime.push({ item: { kind: "failed-follow-up", turn }, at: turn.startedAt, order: turn.firstEventId });
    }
  }
  byTime.sort((a, b) => a.order - b.order);
  for (const placed of afterMessage.values()) placed.sort((a, b) => a.run.firstEventId - b.run.firstEventId);

  const timeline: TimelineItem[] = [];
  let next = 0;
  for (const message of messages) {
    while (next < byTime.length && isBefore(byTime[next].at, message.created_at)) {
      timeline.push(byTime[next].item);
      next += 1;
    }
    timeline.push({ kind: "message", message });
    timeline.push(...(afterMessage.get(message.id) ?? []));
  }
  for (const { item } of byTime.slice(next)) timeline.push(item);
  return timeline;
}

function append(placed: Map<string, RunItem[]>, messageId: string, item: RunItem): void {
  const items = placed.get(messageId);
  if (items === undefined) placed.set(messageId, [item]);
  else items.push(item);
}

/** Whether `a` is earlier than `b`; a time that cannot be read is taken as the latest. */
function isBefore(a: string, b: string): boolean {
  const first = Date.parse(a);
  const second = Date.parse(b);
  if (Number.isNaN(first)) return false;
  if (Number.isNaN(second)) return true;
  return first < second;
}
