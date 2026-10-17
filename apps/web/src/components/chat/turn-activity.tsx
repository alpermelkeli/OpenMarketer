import { CheckIcon, CircleSmallIcon, XIcon } from "lucide-react";

import { Spinner } from "@/components/common/spinner";
import type { TurnStep } from "@/lib/chat/events";
import { cn } from "@/lib/utils";

type TurnActivityProps = {
  /** What the orchestrator is doing, said before any step has arrived. */
  heading: string;
  steps: readonly TurnStep[];
};

/** A turn being answered: what it is doing, and each tool it has called, as they happen. */
export function TurnActivity({ heading, steps }: TurnActivityProps) {
  return (
    // Polite, so a screen reader hears each step without being interrupted.
    <div role="status" aria-live="polite" className="grid gap-2 text-sm text-muted-foreground">
      <p className="flex items-center gap-2">
        {steps.length === 0 && <Spinner />}
        {heading}
      </p>
      {steps.length > 0 && <StepList steps={steps} live />}
    </div>
  );
}

/** The tools an answered turn called, folded away under its answer. */
export function TurnSteps({ steps }: { steps: readonly TurnStep[] }) {
  if (steps.length === 0) return null;
  return (
    <details className="group mt-2 text-xs text-muted-foreground">
      <summary className="w-fit cursor-pointer rounded-sm hover:text-foreground">
        Steps ({steps.length})
      </summary>
      <StepList steps={steps} live={false} className="mt-2" />
    </details>
  );
}

function StepList({ steps, live, className }: { steps: readonly TurnStep[]; live: boolean; className?: string }) {
  return (
    <ol className={cn("grid gap-1.5", className)}>
      {steps.map((step, index) => (
        <li key={`${step.tool}:${index}`} className="flex items-start gap-2">
          <StepMark ok={step.ok} live={live} />
          {/* A label is what the orchestrator wrote: shown as text. */}
          <span className={cn("min-w-0 break-words", step.ok === null && live && "text-foreground")}>
            {step.label}
          </span>
        </li>
      ))}
    </ol>
  );
}

function StepMark({ ok, live }: { ok: boolean | null; live: boolean }) {
  if (ok === true) {
    return (
      <>
        <CheckIcon aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-success" />
        <span className="sr-only">Done:</span>
      </>
    );
  }
  if (ok === false) {
    return (
      <>
        <XIcon aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-destructive" />
        <span className="sr-only">Failed:</span>
      </>
    );
  }
  if (live) {
    return (
      <>
        <Spinner className="mt-0.5" />
        <span className="sr-only">Working:</span>
      </>
    );
  }
  return (
    <>
      <CircleSmallIcon aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
      <span className="sr-only">No answer recorded:</span>
    </>
  );
}
