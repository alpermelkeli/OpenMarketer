/**
 * How a task's progress reads in its chat: its state, a sentence for each node it recorded, where
 * each step of its plan stands, the way to the run each step started, and its report.
 *
 * The task's own record wins over its events once it has ended: a task whose workflow could not be
 * started is recorded as failed without ever recording an event. A run's address is built from its
 * id only after checking it is one, and only for an agent the dashboard knows; a reason, a summary
 * and everything a plan holds are text, shown as such.
 *
 * Display only: what the task does next, and whether it may, is the server's.
 */

import type { Tone } from "@/lib/analysis/run-status";
import type { Proposal, TaskAgentName, TaskDetail, TaskReport, TaskStatus, TaskStepRun } from "@/lib/api/types";
import { isResearchRunId, isStudioRunId, isWritingRunId } from "@/lib/projects/project-id";
import { researchHref, studioHref, writingHref } from "@/lib/projects/project-navigation";
import { type PlanStepView, agentLabel, usd } from "@/lib/tasks/plan";
import { waitingPostProposal } from "@/lib/writing/status";

import type { RunProposal, TaskRunView, TaskStage } from "./events";

export type TaskProgressStatus = {
  state: TaskStatus;
  /** How a task that ended with its report came out, as the record or its last event said. */
  outcome: string | null;
  reason: string | null;
};

export function taskProgressStatus(view: TaskRunView, task: Pick<TaskDetail, "status" | "outcome" | "error"> | undefined): TaskProgressStatus {
  const reported = reportedOutcome(view);
  if (task?.status === "failed") return { state: "failed", outcome: null, reason: task.error ?? view.reason };
  if (task?.status === "succeeded") return { state: "succeeded", outcome: task.outcome ?? reported, reason: null };
  if (view.status === "finished") return { state: "succeeded", outcome: reported, reason: null };
  if (view.status === "failed") return { state: "failed", outcome: null, reason: view.reason };
  const queued = task?.status === "queued" && view.stages.length === 0;
  return { state: queued ? "queued" : "running", outcome: null, reason: null };
}

function reportedOutcome(view: TaskRunView): string | null {
  const reported = view.stages.findLast((stage) => stage.kind === "reported");
  return reported?.kind === "reported" ? reported.outcome : null;
}

type StatusDisplay = { label: string; tone: Tone };

const OUTCOMES = new Map<string, StatusDisplay & { sentence: string }>([
  ["completed", { label: "Done", tone: "success", sentence: "Every step of its plan ran." }],
  [
    "incomplete",
    {
      label: "Ended short of its plan",
      tone: "warning",
      sentence: "It stopped before every step ran: a step failed or was dropped, or a limit ended it.",
    },
  ],
  ["plan_rejected", { label: "Plan rejected", tone: "neutral", sentence: "A person rejected its plan, so nothing ran." }],
  [
    "plan_expired",
    { label: "Plan expired", tone: "neutral", sentence: "Nobody confirmed its plan before its deadline, so nothing ran." },
  ],
]);

/** The task's state in words, with a tone. A plan that waits for the person is what they need to know. */
export function taskStatusDisplay(status: TaskProgressStatus, proposal: RunProposal | null): StatusDisplay {
  if (status.state === "failed") return { label: "Failed", tone: "danger" };
  if (status.state === "succeeded") {
    const outcome = status.outcome === null ? undefined : OUTCOMES.get(status.outcome);
    return outcome === undefined ? { label: "Finished", tone: "success" } : { label: outcome.label, tone: outcome.tone };
  }
  if (status.state === "queued") return { label: "Queued", tone: "neutral" };
  if (proposal?.status === "waiting") return { label: "Waiting for you to confirm", tone: "warning" };
  return { label: "Working", tone: "progress" };
}

/** The sentence that says how a finished task came out; null before it has. */
export function taskOutcomeText(status: TaskProgressStatus): string | null {
  if (status.state !== "succeeded") return null;
  return (status.outcome === null ? undefined : OUTCOMES.get(status.outcome))?.sentence ?? "It ended with its report.";
}

/** Whether the task has ended: it never changes again. */
export function isTaskFinished(task: Pick<TaskDetail, "status">): boolean {
  return task.status === "succeeded" || task.status === "failed";
}

const CONFIRMATIONS = new Map<string, string>([
  ["confirmed", "You confirmed the plan"],
  ["rejected", "The plan was rejected; nothing of it runs"],
  ["expired", "Nobody confirmed the plan in time; nothing of it runs"],
]);

const DECISIONS = new Map<string, string>([
  ["dispatch_next", "Reviewed the step: the plan goes on"],
  ["change_plan", "Reviewed the step: the plan changes"],
  ["finish", "Reviewed the step: the task finishes"],
  ["unread", "Reviewed the step: the review could not be read, so the confirmed plan goes on"],
]);

const STEP_ENDINGS = new Map<string, string>([
  ["succeeded", "finished"],
  ["failed", "failed"],
]);

const POST_ENDINGS = new Map<string, string>([
  ["dry_run", "the post was approved and recorded as a dry run"],
  ["publishing", "the post was approved and is being published"],
  ["published", "the post was published"],
  ["rejected", "the post was rejected"],
  ["expired", "nobody decided on the post in time"],
  ["failed", "the post was not published"],
]);

/** What one node did, as a line of the task's card. A step's run is linked beside it (`stepRunHref`). */
export function taskStageText(stage: TaskStage): string {
  switch (stage.kind) {
    case "planned":
      return stage.agents.length === 0
        ? `Planned ${plural(stage.steps, "step", "steps")}`
        : `Planned ${plural(stage.steps, "step", "steps")}: ${stage.agents.map(agentLabel).join(", then ")}`;
    case "checked": {
      const changed = stage.planChanges === 0 ? "" : ` (changed ${plural(stage.planChanges, "time", "times")})`;
      const confirm = stage.needsConfirmation ? "; it waits for you to confirm it" : "; it needs no confirmation";
      return `Checked the plan${changed}: ${plural(stage.steps, "step", "steps")}${confirm}`;
    }
    case "confirmation":
      return CONFIRMATIONS.get(stage.status) ?? `The plan’s confirmation ended: ${stage.status}`;
    case "step_finished": {
      const head = `Step ${stage.step} (${agentLabel(stage.agent)}) ${STEP_ENDINGS.get(stage.status) ?? stage.status}`;
      const post = stage.postStatus === null ? undefined : POST_ENDINGS.get(stage.postStatus);
      if (stage.runId === null && stage.status === "failed") return `${head} before its run started`;
      return post === undefined ? head : `${head}: ${post}`;
    }
    case "step_dropped":
      return stage.steps.length === 1
        ? `Dropped step ${stage.steps[0]}: what it waited for did not succeed`
        : `Dropped steps ${listed(stage.steps.map(String))}: what they waited for did not succeed`;
    case "reviewed":
      return DECISIONS.get(stage.decision) ?? `Reviewed the step: ${stage.decision}`;
    case "reported":
      return stage.costUsd === null ? "Wrote its report" : `Wrote its report; its own model calls cost ${usd(stage.costUsd)}`;
  }
}

const RUN_HREFS: Record<TaskAgentName, { href: (projectId: string, runId: string) => string; valid: (id: string) => boolean }> = {
  research: { href: researchHref, valid: isResearchRunId },
  studio: { href: studioHref, valid: isStudioRunId },
  writing: { href: writingHref, valid: isWritingRunId },
};

/** The page of the run a step started; null for an agent the dashboard does not know or an id that is none. */
export function stepRunHref(projectId: string, agent: string, runId: string | null): string | null {
  if (runId === null || !Object.hasOwn(RUN_HREFS, agent)) return null;
  const route = RUN_HREFS[agent as TaskAgentName];
  return route.valid(runId) ? route.href(projectId, encodeURIComponent(runId)) : null;
}

const RUN_LINK_LABELS: Record<TaskAgentName, string> = {
  research: "Its report",
  studio: "Its image",
  writing: "Its post",
};

/** The words of the link to the run a step started. */
export function stepRunLinkLabel(agent: string): string {
  return Object.hasOwn(RUN_LINK_LABELS, agent) ? RUN_LINK_LABELS[agent as TaskAgentName] : "Its run";
}

export type StepState = "not_started" | "running" | "succeeded" | "needs_review" | "failed";

export type StepRow = {
  step: PlanStepView;
  /** Its place in the plan, from 1. */
  place: number;
  state: StepState;
  /** The run it started, once it was dispatched. */
  runId: string | null;
};

/**
 * Each step of the plan with where it stands, as the task's record says: one it dispatched is
 * running until its run's outcome is stored. A step that never ran (not reached, dropped, or one
 * that failed before a run started) is not started; the card's lines say why.
 */
export function stepRows(steps: readonly PlanStepView[], dispatched: readonly TaskStepRun[]): StepRow[] {
  return steps.map((step, index) => {
    const run = dispatched.findLast((item) => item.step_id === step.id);
    return {
      step,
      place: index + 1,
      state: run === undefined ? "not_started" : (run.outcome ?? "running"),
      runId: run?.run_id ?? null,
    };
  });
}

const STEP_STATES: Record<StepState, StatusDisplay> = {
  not_started: { label: "Not started", tone: "neutral" },
  running: { label: "Running", tone: "progress" },
  succeeded: { label: "Finished", tone: "success" },
  needs_review: { label: "Needs your review", tone: "warning" },
  failed: { label: "Failed", tone: "danger" },
};

export function stepStateDisplay(state: StepState): StatusDisplay {
  return STEP_STATES[state];
}

/**
 * For each writing step's run whose post waits for a person, the proposal it waits on, among the
 * proposals that wait: the task waits with it, and the card links to where it is decided.
 */
export function waitingPostsOf(waiting: readonly Proposal[], dispatched: readonly TaskStepRun[]): Map<string, string> {
  const posts = new Map<string, string>();
  for (const step of dispatched) {
    if (step.agent !== "writing" || step.outcome !== null) continue;
    const proposal = waitingPostProposal(waiting, step.run_id);
    if (proposal !== null) posts.set(step.run_id, proposal.id);
  }
  return posts;
}

/** What the report links to, counted: "2 posts, 1 image and 1 research report"; null for nothing. */
export function reportMadeText(report: TaskReport): string | null {
  const parts = [
    count(report.content_item_ids, "post", "posts"),
    count(report.asset_ids, "image", "images"),
    count(report.research_run_ids, "research report", "research reports"),
  ].filter((part): part is string => part !== null);
  return parts.length === 0 ? null : `It made ${listed(parts)}.`;
}

/** The research runs whose reports the task used, as pages to open: only ids that are ids. */
export function reportResearchLinks(projectId: string, report: TaskReport): { runId: string; href: string }[] {
  return (report.research_run_ids ?? [])
    .filter(isResearchRunId)
    .map((runId) => ({ runId, href: researchHref(projectId, encodeURIComponent(runId)) }));
}

function count(ids: readonly string[] | undefined, one: string, many: string): string | null {
  const n = ids?.length ?? 0;
  return n === 0 ? null : plural(n, one, many);
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

function listed(items: readonly string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`;
}
