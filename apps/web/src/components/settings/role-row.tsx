"use client";

import { useId, useState, type FormEvent, type ReactNode } from "react";

import { FieldErrors } from "@/components/common/field-errors";
import { InlineCodeText } from "@/components/common/inline-code-text";
import { Notice } from "@/components/common/notice";
import { StatusPill } from "@/components/common/status-pill";
import { OptionSelect } from "@/components/profile/option-select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useRemoveRoleChoice, useSetRoleChoice } from "@/lib/api/model-settings";
import type { ChoiceBounds, ModelProblem, RoleSetting } from "@/lib/api/types";
import { fieldProblems, formProblems, type FormProblems } from "@/lib/settings/form-problems";
import { problemText, roleLabel, roleSourceDisplay } from "@/lib/settings/model-labels";
import {
  roleChoiceOf,
  roleFormOf,
  takesFallbacks,
  type ReasoningEffortField,
  type RoleForm,
} from "@/lib/settings/role-form";

type RoleRowProps = {
  role: RoleSetting;
  /** What the check found wrong with this role's models. */
  problems: readonly ModelProblem[];
  bounds: ChoiceBounds;
  /** The `id` of the list of the catalogue's models this role may take: those that make images, for a
   * role of kind `images`. */
  catalogList: string;
};

const FIELDS = ["model", "fallbacks", "temperature", "max_tokens", "reasoning_effort"];

/**
 * One role: what its requests are made with and where that comes from, what the check
 * found, and the form that chooses its model and parameters. A field left empty keeps
 * the configuration file's value, which the field shows greyed out.
 */
export function RoleRow({ role, problems, bounds, catalogList }: RoleRowProps) {
  const id = useId();
  const label = roleLabel(role.role);
  const source = roleSourceDisplay(role.model_source, role.tier);
  const setChoice = useSetRoleChoice(role.role);
  const removeChoice = useRemoveRoleChoice(role.role);
  const [form, setForm] = useState<RoleForm | null>(null);
  const [typed, setTyped] = useState<FormProblems["fields"]>({});
  const sent = formProblems(setChoice.error ?? removeChoice.error, FIELDS);
  const fieldsProblems: FormProblems = { fields: { ...sent.fields, ...typed }, form: sent.form };
  const pending = setChoice.isPending || removeChoice.isPending;
  const effective = role.effective;
  const isChat = role.kind === "chat";
  const hasFallbacks = takesFallbacks(role.kind);

  function open() {
    setChoice.reset();
    setTyped({});
    setForm(roleFormOf(role.choice));
  }

  function change<Field extends keyof RoleForm>(field: Field, value: RoleForm[Field]) {
    setForm((current) => (current === null ? current : { ...current, [field]: value }));
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (form === null || pending) return;
    const parsed = roleChoiceOf(form);
    setTyped(parsed.problems ?? {});
    if (parsed.choice === null) return;
    removeChoice.reset();
    setChoice.mutate(parsed.choice, { onSuccess: () => setForm(null) });
  }

  function useFilesSettings() {
    setChoice.reset();
    removeChoice.mutate(undefined, { onSuccess: () => setForm(null) });
  }

  return (
    <li className="grid gap-4 py-6 [&_code]:font-mono [&_code]:text-xs">
      <div className="grid gap-1.5">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <h3 className="font-heading text-lg break-words">{label.label}</h3>
          <code className="text-muted-foreground">{role.role}</code>
          {label.inUse && <StatusPill tone="progress">In use</StatusPill>}
        </div>
        <p className="max-w-prose text-sm text-pretty text-muted-foreground">{label.purpose}</p>
      </div>

      <dl aria-live="polite" className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[max-content_1fr]">
        <dt className="text-muted-foreground">Model</dt>
        <dd className="flex flex-wrap items-center gap-2 break-all">
          <code className="text-sm">{effective.model}</code>
          <StatusPill tone={source.tone}>{source.label}</StatusPill>
        </dd>
        {hasFallbacks && (
          <>
            <dt className="text-muted-foreground">Fallbacks</dt>
            <dd className="break-all">
              {effective.fallbacks.length === 0 ? "None" : effective.fallbacks.map((model) => (
                <code key={model} className="mr-2">{model}</code>
              ))}
            </dd>
          </>
        )}
        {isChat && (
          <>
            <dt className="text-muted-foreground">Temperature</dt>
            <dd>{role.temperature_sent ? effective.temperature : "Not sent: the model rejects it"}</dd>
            <dt className="text-muted-foreground">Length limit</dt>
            <dd>{effective.max_tokens.toLocaleString("en")} tokens</dd>
            <dt className="text-muted-foreground">Reasoning effort</dt>
            <dd>{effective.reasoning_effort ?? "The model's own"}</dd>
          </>
        )}
        {role.requires.length > 0 && (
          <>
            <dt className="text-muted-foreground">Needs</dt>
            <dd>{role.requires.map((need) => need.replace("_", " ")).join(", ")}</dd>
          </>
        )}
      </dl>

      {problems.length > 0 && (
        <Notice tone="warning" title="The provider's catalogue says this may not work" className="max-w-prose">
          {problems.map((problem) => (
            <p key={`${problem.model} ${problem.problem}`}>
              <code>{problem.model}</code> {problemText(problem.problem)}
            </p>
          ))}
        </Notice>
      )}

      {form === null ? (
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="outline" onClick={open} aria-expanded={false}>
            Change<span className="sr-only"> the {label.label}</span>
          </Button>
          {role.choice !== null && (
            <Button type="button" variant="ghost" onClick={useFilesSettings} disabled={pending}>
              {removeChoice.isPending ? "Resetting…" : "Use the file’s settings"}
            </Button>
          )}
        </div>
      ) : (
        <form onSubmit={save} className="grid max-w-prose gap-4" autoComplete="off" noValidate>
          <p className="text-sm text-pretty text-muted-foreground">
            A field left empty keeps the configuration file&rsquo;s value, shown greyed out in it.
          </p>

          <Field id={`${id}-model`} label="Model" problems={fieldProblems(fieldsProblems, "model")}>
            <Input
              id={`${id}-model`}
              list={catalogList}
              value={form.model}
              onChange={(event) => change("model", event.target.value)}
              placeholder={role.inherited.model}
              spellCheck={false}
              autoCapitalize="none"
              disabled={pending || role.model_source === "environment"}
              className="font-mono"
              aria-invalid={fieldProblems(fieldsProblems, "model").length > 0}
              aria-describedby={`${id}-model-errors`}
            />
            {role.model_source === "environment" && (
              <p className="text-xs text-muted-foreground">
                <code>{role.environment_variable}</code> sets this role&rsquo;s model in the environment; its other
                settings can still be chosen here.
              </p>
            )}
          </Field>

          {hasFallbacks && (
            <Field
              id={`${id}-fallbacks`}
              label={`Fallbacks (at most ${bounds.max_fallbacks}, one per line)`}
              problems={fieldProblems(fieldsProblems, "fallbacks")}
            >
              <Textarea
                id={`${id}-fallbacks`}
                value={form.fallbacks}
                onChange={(event) => change("fallbacks", event.target.value)}
                placeholder={role.inherited.fallbacks.join("\n") || "None"}
                spellCheck={false}
                rows={2}
                disabled={pending}
                className="font-mono"
                aria-invalid={fieldProblems(fieldsProblems, "fallbacks").length > 0}
                aria-describedby={`${id}-fallbacks-errors`}
              />
            </Field>
          )}

          {isChat && (
            <>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field
                  id={`${id}-temperature`}
                  label={`Temperature (${bounds.min_temperature} to ${bounds.max_temperature})`}
                  problems={fieldProblems(fieldsProblems, "temperature")}
                >
                  <Input
                    id={`${id}-temperature`}
                    inputMode="decimal"
                    value={form.temperature}
                    onChange={(event) => change("temperature", event.target.value)}
                    placeholder={String(role.inherited.temperature)}
                    disabled={pending}
                    aria-invalid={fieldProblems(fieldsProblems, "temperature").length > 0}
                    aria-describedby={`${id}-temperature-errors`}
                  />
                </Field>
                <Field
                  id={`${id}-max_tokens`}
                  label="Length limit, in tokens"
                  problems={fieldProblems(fieldsProblems, "max_tokens")}
                >
                  <Input
                    id={`${id}-max_tokens`}
                    inputMode="numeric"
                    value={form.maxTokens}
                    onChange={(event) => change("maxTokens", event.target.value)}
                    placeholder={String(role.inherited.max_tokens)}
                    disabled={pending}
                    aria-invalid={fieldProblems(fieldsProblems, "max_tokens").length > 0}
                    aria-describedby={`${id}-max_tokens-errors`}
                  />
                </Field>
              </div>
              <div className="grid gap-1.5">
                <span className="text-sm">Reasoning effort</span>
                <OptionSelect<ReasoningEffortField>
                  label="Reasoning effort"
                  value={form.reasoningEffort}
                  options={[
                    { value: "", label: `The file’s (${role.inherited.reasoning_effort ?? "the model’s own"})` },
                    { value: "low", label: "Low" },
                    { value: "medium", label: "Medium" },
                    { value: "high", label: "High" },
                  ]}
                  onChange={(value) => change("reasoningEffort", value)}
                />
                <FieldErrors id={`${id}-reasoning-errors`} messages={fieldProblems(fieldsProblems, "reasoning_effort")} />
              </div>
            </>
          )}

          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={pending}>
              {setChoice.isPending ? "Saving…" : "Save"}
            </Button>
            <Button type="button" variant="outline" onClick={() => setForm(null)} disabled={pending}>
              Cancel
            </Button>
          </div>
        </form>
      )}

      {fieldsProblems.form.length > 0 && (
        <Notice tone="danger" title="The role was not changed" role="alert" className="max-w-prose">
          {fieldsProblems.form.map((problem) => (
            <p key={problem}>
              <InlineCodeText text={problem} />
            </p>
          ))}
        </Notice>
      )}
    </li>
  );
}

type FieldProps = { id: string; label: string; problems: readonly string[]; children: ReactNode };

/** A labelled field with the messages about it below. */
function Field({ id, label, problems, children }: FieldProps) {
  return (
    <div className="grid gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      <FieldErrors id={`${id}-errors`} messages={problems} />
    </div>
  );
}
