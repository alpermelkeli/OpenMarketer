import { CoinsIcon } from "lucide-react";
import type { ReactNode } from "react";

import { type PlanStepView, type PlanView, scopeText } from "@/lib/tasks/plan";

type TaskPlanProps = {
  plan: PlanView;
  /** What to show beside a step: where it stands and the way to its run, when the plan is running. */
  aside?: (step: PlanStepView, place: number) => ReactNode;
};

/**
 * A task's plan, as a numbered list: each step's agent, what it is asked, what it waits for, and
 * whether it can spend. Everything in it was written by a model and is shown as text only.
 */
export function TaskPlan({ plan, aside }: TaskPlanProps) {
  const scope = scopeText(plan.scope);
  return (
    <div className="grid gap-2">
      <p className="text-pretty break-words whitespace-pre-wrap">{plan.goal}</p>
      {plan.steps.length === 0 ? (
        <p className="text-muted-foreground">The plan has no step the dashboard can read.</p>
      ) : (
        <ol className="grid gap-2">
          {plan.steps.map((step, index) => (
            <li key={`${index}-${step.id}`} className="grid gap-1 border-l pl-3">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="font-mono text-xs text-muted-foreground tabular-nums">{index + 1}</span>
                <span className="text-2xs tracking-widest text-muted-foreground uppercase">{step.agentLabel}</span>
                <span className="font-mono text-xs break-all text-muted-foreground">{step.id}</span>
                {step.canSpend && (
                  <span className="inline-flex items-center gap-1 text-xs text-warning">
                    <CoinsIcon aria-hidden="true" className="size-3.5" />
                    Can spend: makes images the creative budget pays for
                  </span>
                )}
                {aside?.(step, index + 1)}
              </div>
              {step.asks.length > 0 && (
                <dl className="grid gap-0.5 text-xs">
                  {step.asks.map((ask) => (
                    <div key={ask.label} className="grid gap-x-2 sm:grid-cols-[8rem_1fr]">
                      <dt className="text-muted-foreground">{ask.label}</dt>
                      <dd className="min-w-0">
                        {ask.lines.map((line, lineIndex) => (
                          <p key={lineIndex} className="break-words whitespace-pre-wrap">
                            {line}
                          </p>
                        ))}
                      </dd>
                    </div>
                  ))}
                </dl>
              )}
              {step.dependsOn.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  Waits for{" "}
                  {step.dependsOn.map((id, idIndex) => (
                    <span key={idIndex}>
                      {idIndex > 0 && ", "}
                      <span className="font-mono break-all">{id}</span>
                    </span>
                  ))}
                </p>
              )}
            </li>
          ))}
        </ol>
      )}
      {scope !== null && <p className="text-xs text-muted-foreground">{scope}</p>}
    </div>
  );
}
