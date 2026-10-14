"use client";

import { useId, useState, type FormEvent } from "react";

import { FieldErrors } from "@/components/common/field-errors";
import { InlineCodeText } from "@/components/common/inline-code-text";
import { Notice } from "@/components/common/notice";
import { RequestProblem } from "@/components/common/request-problem";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { useAnalysisSettings, useRemoveAnalysisLimits, useSetAnalysisLimits } from "@/lib/api/analysis-settings";
import type { AnalysisSettings } from "@/lib/api/types";
import { analysisFormOf, analysisLimitsOf, type AnalysisForm } from "@/lib/settings/analysis-form";
import { fieldProblems, formProblems, type FormProblems } from "@/lib/settings/form-problems";

const DOLLARS = new Intl.NumberFormat("en", { style: "currency", currency: "USD" });

/** The analysis section: the limits a run gets, and the form that chooses them. */
export function AnalysisScreen() {
  const settings = useAnalysisSettings();
  const data = settings.data;

  return (
    <section aria-labelledby="analysis" className="grid gap-6">
      <div>
        <h2 id="analysis" className="font-heading text-title">
          Analysis
        </h2>
        <p className="mt-2 max-w-prose text-pretty text-muted-foreground">
          How far the analyzer may go on one repository. When a run reaches a limit it stops without a profile, so a
          limit is also what a run can cost at most. A change applies from a run&rsquo;s next step on.
        </p>
      </div>

      {settings.error !== null && (
        <RequestProblem
          title="The limits could not be read"
          error={settings.error}
          onRetry={() => void settings.refetch()}
        />
      )}

      {data === undefined && settings.error === null && (
        <Skeleton className="h-48" aria-busy="true" aria-label="Loading the limits" />
      )}

      {data !== undefined && <LimitsForm key={data.set_at ?? "defaults"} settings={data} />}
    </section>
  );
}

type LimitsFormProps = { settings: AnalysisSettings };

function LimitsForm({ settings }: LimitsFormProps) {
  const id = useId();
  const setLimits = useSetAnalysisLimits();
  const removeLimits = useRemoveAnalysisLimits();
  const [form, setForm] = useState<AnalysisForm>(analysisFormOf(settings.chosen));
  const [typed, setTyped] = useState<FormProblems["fields"]>({});
  const sent = formProblems(setLimits.error ?? removeLimits.error, ["max_steps", "max_cost_usd"]);
  const problems: FormProblems = { fields: { ...sent.fields, ...typed }, form: sent.form };
  const pending = setLimits.isPending || removeLimits.isPending;
  const { limits, defaults, bounds } = settings;

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    const parsed = analysisLimitsOf(form);
    setTyped(parsed.problems ?? {});
    if (parsed.limits === null) return;
    removeLimits.reset();
    setLimits.mutate(parsed.limits);
  }

  function useDefaults() {
    setLimits.reset();
    removeLimits.mutate();
  }

  return (
    <div className="grid gap-6">
      {settings.unreadable && (
        <Notice tone="danger" title="The stored limits are not valid any more" role="alert" className="max-w-prose">
          Analyses fail until the limits are set again or reset to the defaults.
        </Notice>
      )}

      <dl aria-live="polite" className="grid max-w-prose gap-x-6 gap-y-2 text-sm sm:grid-cols-[max-content_1fr]">
        <dt className="text-muted-foreground">Steps</dt>
        <dd>
          {limits.max_steps} model turns{limits.max_steps !== defaults.max_steps && ` (default ${defaults.max_steps})`}
        </dd>
        <dt className="text-muted-foreground">Cost</dt>
        <dd>
          {DOLLARS.format(limits.max_cost_usd)} per run
          {limits.max_cost_usd !== defaults.max_cost_usd && ` (default ${DOLLARS.format(defaults.max_cost_usd)})`}
        </dd>
      </dl>

      <form onSubmit={save} className="grid max-w-prose gap-4" autoComplete="off" noValidate>
        <p className="text-sm text-pretty text-muted-foreground">A field left empty keeps the default.</p>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="grid gap-1.5">
            <Label htmlFor={`${id}-steps`}>
              Steps ({bounds.min_steps} to {bounds.max_steps})
            </Label>
            <Input
              id={`${id}-steps`}
              inputMode="numeric"
              value={form.maxSteps}
              onChange={(event) => setForm({ ...form, maxSteps: event.target.value })}
              placeholder={String(defaults.max_steps)}
              disabled={pending}
              aria-invalid={fieldProblems(problems, "max_steps").length > 0}
              aria-describedby={`${id}-steps-errors`}
            />
            <FieldErrors id={`${id}-steps-errors`} messages={fieldProblems(problems, "max_steps")} />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor={`${id}-cost`}>
              Cost in US dollars ({bounds.min_cost_usd} to {bounds.max_cost_usd})
            </Label>
            <Input
              id={`${id}-cost`}
              inputMode="decimal"
              value={form.maxCostUsd}
              onChange={(event) => setForm({ ...form, maxCostUsd: event.target.value })}
              placeholder={String(defaults.max_cost_usd)}
              disabled={pending}
              aria-invalid={fieldProblems(problems, "max_cost_usd").length > 0}
              aria-describedby={`${id}-cost-errors`}
            />
            <FieldErrors id={`${id}-cost-errors`} messages={fieldProblems(problems, "max_cost_usd")} />
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button type="submit" disabled={pending}>
            {setLimits.isPending ? "Saving…" : "Save"}
          </Button>
          {(settings.chosen !== null || settings.unreadable) && (
            <Button type="button" variant="outline" onClick={useDefaults} disabled={pending}>
              {removeLimits.isPending ? "Resetting…" : "Use the defaults"}
            </Button>
          )}
        </div>
      </form>

      {problems.form.length > 0 && (
        <Notice tone="danger" title="The limits were not changed" role="alert" className="max-w-prose">
          {problems.form.map((problem) => (
            <p key={problem}>
              <InlineCodeText text={problem} />
            </p>
          ))}
        </Notice>
      )}
    </div>
  );
}
