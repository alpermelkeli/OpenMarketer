"use client";

import { useId, useState, type FormEvent } from "react";

import { FieldErrors } from "@/components/common/field-errors";
import { InlineCodeText } from "@/components/common/inline-code-text";
import { Notice } from "@/components/common/notice";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useSetRejectedParameters } from "@/lib/api/model-settings";
import type { RejectedParameters as RejectedParametersSetting } from "@/lib/api/types";
import { fieldProblems, formProblems } from "@/lib/settings/form-problems";

type RejectedParametersProps = {
  rejected: readonly RejectedParametersSetting[];
  /** The `id` of the list of the catalogue's models. */
  catalogList: string;
};

/**
 * The models whose requests leave the temperature out, because their providers refuse a
 * request that carries one. A model the configuration file lists stays listed.
 */
export function RejectedParameters({ rejected, catalogList }: RejectedParametersProps) {
  const id = useId();
  const setRejected = useSetRejectedParameters();
  const [model, setModel] = useState("");
  const problems = formProblems(setRejected.error, ["model", "parameters"]);
  const pending = setRejected.isPending;

  function mark(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (model.trim() === "" || pending) return;
    setRejected.mutate({ model: model.trim(), parameters: ["temperature"] }, { onSuccess: () => setModel("") });
  }

  return (
    <section aria-labelledby={`${id}-heading`} className="grid gap-4 [&_code]:font-mono [&_code]:text-xs">
      <div>
        <h3 id={`${id}-heading`} className="font-heading text-lg">
          Models that reject the temperature
        </h3>
        <p className="mt-1 max-w-prose text-sm text-pretty text-muted-foreground">
          Some models refuse a request that sets a temperature, and the provider then finds no one to answer it. A
          request to a model listed here is sent without one.
        </p>
      </div>

      {rejected.length > 0 && (
        <ul className="divide-y border-y text-sm">
          {rejected.map((entry) => (
            <li key={entry.model} className="flex flex-wrap items-center justify-between gap-2 py-3">
              <code className="text-sm break-all">{entry.model}</code>
              {entry.from_file.length > 0 ? (
                <span className="text-muted-foreground">Listed in the configuration file</span>
              ) : (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={pending}
                  onClick={() => setRejected.mutate({ model: entry.model, parameters: [] })}
                >
                  Send the temperature again<span className="sr-only"> to {entry.model}</span>
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}

      <form onSubmit={mark} className="grid max-w-prose gap-2" autoComplete="off" noValidate>
        <Label htmlFor={`${id}-model`}>Model</Label>
        <div className="flex flex-wrap gap-2">
          <Input
            id={`${id}-model`}
            list={catalogList}
            value={model}
            onChange={(event) => setModel(event.target.value)}
            placeholder="provider/model"
            spellCheck={false}
            autoCapitalize="none"
            disabled={pending}
            className="min-w-48 flex-1 font-mono"
            aria-invalid={fieldProblems(problems, "model").length > 0}
            aria-describedby={`${id}-errors`}
          />
          <Button type="submit" disabled={pending || model.trim() === ""}>
            Leave the temperature out
          </Button>
        </div>
        <FieldErrors id={`${id}-errors`} messages={fieldProblems(problems, "model")} />
      </form>

      {problems.form.length > 0 && (
        <Notice tone="danger" title="The list was not changed" role="alert" className="max-w-prose">
          {problems.form.map((problem) => (
            <p key={problem}>
              <InlineCodeText text={problem} />
            </p>
          ))}
        </Notice>
      )}
    </section>
  );
}
