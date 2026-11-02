import { ArrowRightIcon } from "lucide-react";
import Link from "next/link";

import { Notice } from "@/components/common/notice";
import { Spinner } from "@/components/common/spinner";
import { StatusPill } from "@/components/common/status-pill";
import { TaskPlan } from "@/components/tasks/task-plan";
import type { TaskDetail } from "@/lib/api/types";
import type { TaskRunView } from "@/lib/chat/events";
import {
  type StepRow,
  type TaskProgressStatus,
  reportMadeText,
  reportResearchLinks,
  stepRows,
  stepRunHref,
  stepRunLinkLabel,
  stepStateDisplay,
  taskOutcomeText,
  taskStageText,
  taskStatusDisplay,
} from "@/lib/chat/task-progress";
import { shortId } from "@/lib/format";
import { proposalHref } from "@/lib/projects/project-navigation";
import { type PlanView, readPlan, usd } from "@/lib/tasks/plan";

import { RunProposalDecision } from "./run-proposal-decision";

type TaskProgressCardProps = {
  projectId: string;
  view: TaskRunView;
  status: TaskProgressStatus;
  /** The task as its route serves it; undefined until it has been read. */
  task: TaskDetail | undefined;
  /** The plan its waiting `confirm_plan` asks the person to confirm, read from the proposal; null when none waits or it is not loaded. */
  planToConfirm: PlanView | null;
  /** For each writing step's run whose post waits for a person, the proposal it waits on. */
  waitingPosts: ReadonlyMap<string, string>;
};

/**
 * A task a turn of the chat started, as it goes: its plan with each step's agent and what it is
 * asked, the plan to confirm with the buttons to decide it while one waits, where each step
 * stands with the way to the run it started, each node it recorded, and its report once it has
 * ended. A writing step's post is decided where it already is (Approvals, the writing run's page);
 * the card links there while it waits. Everything a plan or a report holds is text.
 */
export function TaskProgressCard({ projectId, view, status, task, planToConfirm, waitingPosts }: TaskProgressCardProps) {
  const display = taskStatusDisplay(status, view.proposal);
  const ended = status.state === "succeeded" || status.state === "failed";
  const confirming = view.proposal?.status === "waiting";
  const plan = task === undefined ? null : readPlan(task.plan);
  const rows = plan === null || task === undefined ? [] : stepRows(plan.steps, task.steps);
  const outcome = taskOutcomeText(status);
  const lastStage = view.stages.at(-1) ?? null;
  const linesLabel = `What it did (${view.stages.length})`;
  const shownToConfirm = planToConfirm ?? plan;

  return (
    <article
      aria-label={`Task ${shortId(view.id)}`}
      className="grid w-full max-w-[min(42rem,100%)] gap-3 justify-self-start rounded-md border bg-card px-4 py-3 text-sm"
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <p className="text-2xs tracking-widest text-muted-foreground uppercase">Task</p>
        <StatusPill tone={display.tone} live={!ended && !confirming}>
          {display.label}
        </StatusPill>
      </header>

      {view.proposal !== null && (
        <RunProposalDecision projectId={projectId} proposal={view.proposal}>
          {confirming && shownToConfirm !== null && (
            <div className="grid gap-1.5">
              <p className="text-2xs tracking-widest text-muted-foreground uppercase">
                {planToConfirm === null ? "The plan, as the task records it" : "The plan you are asked to confirm"}
              </p>
              <TaskPlan plan={shownToConfirm} />
            </div>
          )}
        </RunProposalDecision>
      )}

      {/* Polite, so a screen reader hears the task move on without being interrupted. */}
      <div aria-live="polite" className="grid gap-3">
        {plan === null && !ended && (
          <p className="text-muted-foreground">
            {status.state === "queued"
              ? "Waiting for a worker to pick it up."
              : "Planning: the orchestrator works out which agents the request needs."}
          </p>
        )}

        {plan !== null && !confirming && (
          <section aria-label="Plan" className="grid gap-1.5">
            <p className="text-2xs tracking-widest text-muted-foreground uppercase">Plan</p>
            <TaskPlan
              plan={plan}
              aside={(_, place) => {
                const row = rows[place - 1];
                return row === undefined ? null : (
                  <StepAside projectId={projectId} row={row} waitingPosts={waitingPosts} />
                );
              }}
            />
          </section>
        )}

        {view.stages.length > 0 && (
          <details open={!ended || undefined} className="group">
            <summary className="w-fit cursor-pointer rounded-sm text-muted-foreground hover:text-foreground">
              <span className="inline-flex min-w-0 items-center gap-1.5 group-open:hidden">
                {!ended && !confirming && lastStage !== null && (
                  <>
                    <Spinner />
                    <span className="sr-only">Working: </span>
                  </>
                )}
                <span className="min-w-0 break-words">
                  {!ended && lastStage !== null ? taskStageText(lastStage) : linesLabel}
                </span>
              </span>
              <span className="hidden group-open:inline">{linesLabel}</span>
            </summary>
            <ol className="mt-2 grid gap-1.5 text-muted-foreground">
              {view.stages.map((stage, index) => {
                const href = stage.kind === "step_finished" ? stepRunHref(projectId, stage.agent, stage.runId) : null;
                return (
                  <li key={index} className="min-w-0 break-words">
                    {taskStageText(stage)}
                    {href !== null && stage.kind === "step_finished" && (
                      <>
                        {" · "}
                        <Link href={href} className="rounded-sm text-brand underline-offset-4 hover:underline">
                          {stepRunLinkLabel(stage.agent)}
                        </Link>
                      </>
                    )}
                  </li>
                );
              })}
            </ol>
          </details>
        )}

        {task?.report != null && <TaskReportSection projectId={projectId} task={task} />}
        {outcome !== null && <p>{outcome}</p>}
        {status.state === "failed" && (
          <Notice tone="danger" title="The task failed" role="status">
            <p className="break-words whitespace-pre-wrap text-foreground">{status.reason ?? "No reason was recorded."}</p>
            <p className="mt-2">What its steps made before it failed is still on their runs&rsquo; pages.</p>
          </Notice>
        )}
      </div>
    </article>
  );
}

function StepAside({
  projectId,
  row,
  waitingPosts,
}: {
  projectId: string;
  row: StepRow;
  waitingPosts: ReadonlyMap<string, string>;
}) {
  const state = stepStateDisplay(row.state);
  const href = stepRunHref(projectId, row.step.agent, row.runId);
  const waitingProposal = row.runId === null ? undefined : waitingPosts.get(row.runId);
  return (
    <span className="ml-auto inline-flex flex-wrap items-center gap-x-2 gap-y-1">
      {waitingProposal !== undefined ? (
        <Link
          href={proposalHref(projectId, waitingProposal)}
          className="rounded-sm text-xs text-warning underline-offset-4 hover:underline"
        >
          Its post waits for your decision in Approvals
        </Link>
      ) : (
        <StatusPill tone={state.tone} live={row.state === "running"}>
          {state.label}
        </StatusPill>
      )}
      {href !== null && (
        <Link
          href={href}
          className="inline-flex items-center gap-1 rounded-sm text-xs text-brand underline-offset-4 hover:underline"
        >
          {stepRunLinkLabel(row.step.agent)}
          <ArrowRightIcon aria-hidden="true" className="size-3" />
        </Link>
      )}
    </span>
  );
}

function TaskReportSection({ projectId, task }: { projectId: string; task: TaskDetail }) {
  const report = task.report;
  if (report === null) return null;
  const made = reportMadeText(report);
  const research = reportResearchLinks(projectId, report);
  const rejected = report.rejected ?? [];
  return (
    <section aria-label="Report" className="grid gap-2 border-t pt-3">
      <p className="text-2xs tracking-widest text-muted-foreground uppercase">Report</p>
      {made !== null && <p>{made} Each is on the page of the step that made it, linked in the plan above.</p>}
      {research.length > 0 && (
        <ul className="grid gap-1">
          {research.map((link) => (
            <li key={link.runId}>
              <Link href={link.href} className="rounded-sm text-brand underline-offset-4 hover:underline">
                Research report <span className="font-mono">{shortId(link.runId)}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
      {rejected.length > 0 && (
        <div className="grid gap-1">
          <p className="text-muted-foreground">Turned down</p>
          <ul className="grid gap-0.5">
            {rejected.map((line, index) => (
              <li key={index} className="break-words whitespace-pre-wrap">
                {line}
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="text-muted-foreground">
        The orchestrator&rsquo;s own model calls cost <span className="font-mono tabular-nums">{usd(task.cost_usd)}</span>;
        the summary below counts what its runs cost too.
      </p>
      <details>
        <summary className="w-fit cursor-pointer rounded-sm text-muted-foreground hover:text-foreground">
          The summary, as code wrote it
        </summary>
        {/* Code's text, with a goal and errors a model or a run wrote in it: text only. */}
        <pre className="mt-2 max-h-80 overflow-auto rounded-sm border bg-surface p-3 font-mono text-xs break-words whitespace-pre-wrap">
          {report.summary}
        </pre>
      </details>
    </section>
  );
}
