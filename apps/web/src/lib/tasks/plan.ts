/**
 * A task's plan as the dashboard shows it: its goal, its scope, and each step with its agent and
 * what that agent is asked, line by line.
 *
 * A plan reaches the dashboard twice: typed, as a task's own route serves it, and untyped, as the
 * payload of the `confirm_plan` a task holds for a person (`Proposal.payload`, a JSON object the
 * API does not describe). Both are read here, by the one defensive reader, so the screen never
 * trusts the shape of either: a value of the wrong type is taken as missing, a step of an agent
 * the dashboard does not know is kept with its name and nothing it asks, and a payload that is no
 * plan at all is null. Everything in a plan was written by a model (the goal, each step's id, its
 * questions, its words): it is kept as text for the screen to show as such, never as markup or a
 * link.
 *
 * Which steps can spend is the server's to enforce; a studio step is flagged here only so that the
 * person confirming a plan sees which of its steps make images, which the creative budget pays for.
 *
 * Display only.
 */

import type { Proposal, TaskAgentName } from "@/lib/api/types";
import { platformLabel } from "@/lib/studio/status";
import { goalLabel, writingPlatformLabel } from "@/lib/writing/status";

/** One thing a step is asked: a label, and its value as one or more lines of text. */
export type StepAsk = { label: string; lines: string[] };

export type PlanStepView = {
  /** The step's id in the plan, as the model wrote it. */
  id: string;
  /** The agent, as the plan named it; one the dashboard does not know is kept as it came. */
  agent: string;
  /** The agent in words. */
  agentLabel: string;
  /** The steps it waits for, by id. */
  dependsOn: string[];
  asks: StepAsk[];
  /** Whether its agent makes images, which the creative budget pays for. */
  canSpend: boolean;
};

export type PlanScopeView = {
  maxSteps: number | null;
  maxPlanChanges: number | null;
  deadlineDays: number | null;
  maxCostUsd: number | null;
};

export type PlanView = { goal: string; steps: PlanStepView[]; scope: PlanScopeView | null };

const AGENT_LABELS: Record<TaskAgentName, string> = {
  research: "Research",
  studio: "Studio",
  writing: "Writing",
};

/** The agent in words; one the dashboard does not know yet is shown by its name. */
export function agentLabel(agent: string): string {
  return Object.hasOwn(AGENT_LABELS, agent) ? AGENT_LABELS[agent as TaskAgentName] : agent;
}

/** The run kind and action of the plan a task asks a person to confirm. */
export const TASK_RUN_KIND = "orchestrator-task";
export const CONFIRM_PLAN = "confirm_plan";

/** Whether a proposal is a task's plan waiting to be confirmed, or one that was. */
export function isPlanConfirmation(proposal: Pick<Proposal, "action_name" | "run_kind">): boolean {
  return proposal.action_name === CONFIRM_PLAN && proposal.run_kind === TASK_RUN_KIND;
}

/**
 * The plan a task's `confirm_plan` asks a person to confirm, read from its payload: what the person
 * confirms is in the proposal itself. Null for any other proposal and for a payload with no plan.
 */
export function proposedPlanOf(proposal: Pick<Proposal, "action_name" | "run_kind" | "payload">): PlanView | null {
  return isPlanConfirmation(proposal) ? readPlan(proposal.payload.plan) : null;
}

/** A plan, from the task's own route or from a proposal's payload; null when it is none. */
export function readPlan(value: unknown): PlanView | null {
  if (!isRecord(value)) return null;
  const goal = textOf(value.goal);
  if (goal === null || !Array.isArray(value.steps)) return null;
  const steps = value.steps.map(readStep).filter((step): step is PlanStepView => step !== null);
  return { goal, steps, scope: readScope(value.scope) };
}

function readStep(value: unknown): PlanStepView | null {
  if (!isRecord(value)) return null;
  const id = textOf(value.id);
  const agent = textOf(value.agent);
  if (id === null || agent === null) return null;
  const request = isRecord(value.request) ? value.request : {};
  return {
    id,
    agent,
    agentLabel: agentLabel(agent),
    dependsOn: textsOf(value.depends_on),
    asks: asksOf(agent, request, value),
    canSpend: agent === "studio",
  };
}

function asksOf(agent: string, request: Record<string, unknown>, step: Record<string, unknown>): StepAsk[] {
  switch (agent) {
    case "research":
      return present([
        ask("Questions", textsOf(request.questions)),
        ask("Topics", textsOf(request.topics)),
        ask("Competitors", textsOf(request.competitors)),
        ask("Time window", single(request.time_window)),
        ask("Sources", countText(request.max_sources, "at most")),
      ]);
    case "studio":
      return present([
        ask("Goal", single(request.goal)),
        ask("Platform", single(request.platform).map(platformLabel)),
        ask("Words on the image", textsOf(request.exact_copy).map((line) => `“${line}”`)),
        ask("Style", single(request.style_notes)),
      ]);
    case "writing":
      return present([
        ask("Goal", single(request.goal).map(goalLabel)),
        ask("Platform", single(request.platform).map(writingPlatformLabel)),
        ask("Feature", single(request.feature)),
        ask("Angle", single(request.angle)),
        ask("Variants", countText(request.variants)),
        ask("Image", single(step.asset_from_step).map((id) => `the image of step “${id}”`)),
        ask("Research", single(step.research_from_step).map((id) => `the findings of step “${id}”`)),
      ]);
    default:
      return [];
  }
}

function readScope(value: unknown): PlanScopeView | null {
  if (!isRecord(value)) return null;
  return {
    maxSteps: countOf(value.max_steps),
    maxPlanChanges: countOf(value.max_plan_changes),
    deadlineDays: countOf(value.deadline_days),
    maxCostUsd: amountOf(value.max_cost_usd),
  };
}

/** The scope as one sentence; null when nothing of it could be read. */
export function scopeText(scope: PlanScopeView | null): string | null {
  if (scope === null) return null;
  const parts = [
    scope.maxSteps === null ? null : `at most ${plural(scope.maxSteps, "step", "steps")}`,
    scope.maxPlanChanges === null ? null : `${plural(scope.maxPlanChanges, "change", "changes")} of plan`,
    scope.deadlineDays === null ? null : `${plural(scope.deadlineDays, "day", "days")} to finish`,
    scope.maxCostUsd === null ? null : `${usd(scope.maxCostUsd)} for the orchestrator’s own model calls`,
  ].filter((part): part is string => part !== null);
  if (parts.length === 0) return null;
  return `Within ${parts.join(", ")}. Each run it starts keeps its own limits.`;
}

const USD = new Intl.NumberFormat("en", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

/** An amount in USD: "$0.12". */
export function usd(amount: number): string {
  return USD.format(amount);
}

function ask(label: string, lines: string[]): StepAsk {
  return { label, lines };
}

function present(asks: StepAsk[]): StepAsk[] {
  return asks.filter((item) => item.lines.length > 0);
}

function single(value: unknown): string[] {
  const text = textOf(value);
  return text === null || text.trim() === "" ? [] : [text];
}

function countText(value: unknown, prefix?: string): string[] {
  const count = countOf(value);
  if (count === null) return [];
  return [prefix === undefined ? String(count) : `${prefix} ${count}`];
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

function textOf(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function textsOf(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.trim() !== "") : [];
}

function countOf(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

/** A non-negative amount, as a number or as the text of a decimal (how the API writes one). */
function amountOf(value: unknown): number | null {
  const amount = typeof value === "string" && value.trim() !== "" ? Number(value) : value;
  return typeof amount === "number" && Number.isFinite(amount) && amount >= 0 ? amount : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
