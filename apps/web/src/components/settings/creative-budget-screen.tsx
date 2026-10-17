"use client";

import { useId, useState, type FormEvent } from "react";

import { FieldErrors } from "@/components/common/field-errors";
import { InlineCodeText } from "@/components/common/inline-code-text";
import { Notice } from "@/components/common/notice";
import { RequestProblem } from "@/components/common/request-problem";
import { StatusPill } from "@/components/common/status-pill";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { useCreativeBudget, useRemoveCreativeBudget, useSetCreativeBudget } from "@/lib/api/creative-budget";
import type { CreativeBudget, CreativeBudgetCap } from "@/lib/api/types";
import {
  capSourceDisplay,
  creativeBudgetFormOf,
  creativeBudgetRequestOf,
  formatDollars,
  spendLines,
  spendMonth,
  type CreativeBudgetForm,
} from "@/lib/settings/creative-budget";
import { fieldProblems, formProblems, type FormProblems } from "@/lib/settings/form-problems";

/** The creative budget: what generated media may cost in a month and per action, and what this month has spent. */
export function CreativeBudgetScreen() {
  const budget = useCreativeBudget();
  const data = budget.data;

  return (
    <section aria-labelledby="creative-budget" className="grid gap-6">
      <div>
        <h2 id="creative-budget" className="font-heading text-title">
          Creative budget
        </h2>
        <p className="mt-2 max-w-prose text-pretty text-muted-foreground">
          What the studio&rsquo;s images may cost. An action that spends no more than the automatic limit runs at
          once; one that costs more waits in a project&rsquo;s Approvals for a person. Nothing is spent beyond the
          monthly cap, approved or not. A change applies to the next action that spends.
        </p>
      </div>

      {budget.error !== null && (
        <RequestProblem title="The budget could not be read" error={budget.error} onRetry={() => void budget.refetch()} />
      )}

      {data === undefined && budget.error === null && (
        <Skeleton className="h-48" aria-busy="true" aria-label="Loading the budget" />
      )}

      {data !== undefined && (
        <>
          <MonthSpending budget={data} />
          <CapsForm key={data.set_at ?? "defaults"} budget={data} />
        </>
      )}
    </section>
  );
}

function MonthSpending({ budget }: { budget: CreativeBudget }) {
  const spend = budget.spent_this_month;
  return (
    <div className="grid max-w-prose gap-2 border-t pt-6">
      <h3 className="font-heading text-lg">{spendMonth(spend)}</h3>
      <dl aria-live="polite" className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-[max-content_1fr]">
        {spendLines(spend).map((line) => (
          <div key={line.label} className="contents">
            <dt className="text-muted-foreground">{line.label}</dt>
            <dd className="font-mono tabular-nums">{line.value}</dd>
          </div>
        ))}
      </dl>
      <p className="text-xs text-pretty text-muted-foreground">
        Counted from what the studio recorded for each image, in UTC months. The provider&rsquo;s own bill may differ
        a little; this dashboard cannot see it.
      </p>
    </div>
  );
}

type CapsFormProps = { budget: CreativeBudget };

function CapsForm({ budget }: CapsFormProps) {
  const id = useId();
  const setBudget = useSetCreativeBudget();
  const removeBudget = useRemoveCreativeBudget();
  const [form, setForm] = useState<CreativeBudgetForm>(creativeBudgetFormOf(budget.chosen));
  const [typed, setTyped] = useState<FormProblems["fields"]>({});
  const sent = formProblems(setBudget.error ?? removeBudget.error, ["monthly_usd", "auto_per_action_usd"]);
  const problems: FormProblems = { fields: { ...sent.fields, ...typed }, form: sent.form };
  const pending = setBudget.isPending || removeBudget.isPending;
  const bothFromEnvironment =
    budget.monthly.source === "environment" && budget.auto_per_action.source === "environment";

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    const parsed = creativeBudgetRequestOf(form, budget);
    setTyped(parsed.problems ?? {});
    if (parsed.caps === null) return;
    removeBudget.reset();
    setBudget.mutate(parsed.caps);
  }

  function useDefaults() {
    setBudget.reset();
    removeBudget.mutate();
  }

  return (
    <div className="grid gap-6 border-t pt-6">
      {budget.unreadable && (
        <Notice tone="danger" title="The stored caps are not valid any more" role="alert" className="max-w-prose">
          Nothing is spent until the caps are set again or reset to the defaults.
        </Notice>
      )}

      <form onSubmit={save} className="grid max-w-prose gap-5" autoComplete="off" noValidate>
        <p className="text-sm text-pretty text-muted-foreground">A field left empty keeps the default.</p>
        <CapField
          id={`${id}-monthly`}
          label={`Monthly cap in US dollars (0 to ${formatDollars(budget.bounds.max_monthly_usd)})`}
          cap={budget.monthly}
          value={form.monthlyUsd}
          onChange={(monthlyUsd) => setForm({ ...form, monthlyUsd })}
          disabled={pending}
          problems={fieldProblems(problems, "monthly_usd")}
        />
        <CapField
          id={`${id}-per-action`}
          label={`Automatic limit per action in US dollars (0 to ${formatDollars(budget.bounds.max_auto_per_action_usd)})`}
          hint="An action that costs more than this asks for your approval first. Zero asks for every one."
          cap={budget.auto_per_action}
          value={form.autoPerActionUsd}
          onChange={(autoPerActionUsd) => setForm({ ...form, autoPerActionUsd })}
          disabled={pending}
          problems={fieldProblems(problems, "auto_per_action_usd")}
        />
        <div className="flex flex-wrap gap-2">
          <Button type="submit" disabled={pending || bothFromEnvironment}>
            {setBudget.isPending ? "Saving…" : "Save"}
          </Button>
          {(budget.chosen !== null || budget.unreadable) && (
            <Button type="button" variant="outline" onClick={useDefaults} disabled={pending}>
              {removeBudget.isPending ? "Resetting…" : "Use the defaults"}
            </Button>
          )}
        </div>
      </form>

      {problems.form.length > 0 && (
        <Notice tone="danger" title="The caps were not changed" role="alert" className="max-w-prose">
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

type CapFieldProps = {
  id: string;
  label: string;
  hint?: string;
  cap: CreativeBudgetCap;
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
  problems: readonly string[];
};

/** One cap: the value in use and where it comes from, and the field that chooses it unless the environment does. */
function CapField({ id, label, hint, cap, value, onChange, disabled, problems }: CapFieldProps) {
  const source = capSourceDisplay(cap.source);
  const fromEnvironment = cap.source === "environment";
  return (
    <div className="grid gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        <span className="font-mono tabular-nums">{formatDollars(cap.value_usd)} in use</span>
        <StatusPill tone={source.tone}>{source.label}</StatusPill>
        {cap.value_usd !== cap.default_usd && (
          <span className="text-xs text-muted-foreground">default {formatDollars(cap.default_usd)}</span>
        )}
      </div>
      <Input
        id={id}
        inputMode="decimal"
        value={fromEnvironment ? "" : value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={fromEnvironment ? String(cap.value_usd) : String(cap.default_usd)}
        disabled={disabled || fromEnvironment}
        aria-invalid={problems.length > 0}
        aria-describedby={`${id}-hint ${id}-errors`}
        className="font-mono"
      />
      <p id={`${id}-hint`} className="text-xs text-pretty text-muted-foreground">
        {fromEnvironment ? (
          <>
            Set by <code className="font-mono text-foreground">{cap.environment_variable}</code> in the environment,
            which wins over a choice here. Unset it to choose here.
          </>
        ) : (
          hint
        )}
      </p>
      <FieldErrors id={`${id}-errors`} messages={problems} />
    </div>
  );
}
