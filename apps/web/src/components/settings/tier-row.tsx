"use client";

import { useId, useState, type FormEvent } from "react";

import { FieldErrors } from "@/components/common/field-errors";
import { InlineCodeText } from "@/components/common/inline-code-text";
import { Notice } from "@/components/common/notice";
import { StatusPill } from "@/components/common/status-pill";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useRemoveTierModel, useSetTierModel } from "@/lib/api/model-settings";
import type { TierSetting } from "@/lib/api/types";
import { fieldProblems, formProblems } from "@/lib/settings/form-problems";
import { tierPurpose, tierSourceDisplay } from "@/lib/settings/model-labels";

type TierRowProps = {
  tier: TierSetting;
  /** The `id` of the list of the catalogue's models. */
  catalogList: string;
};

/** One tier: its model, where that comes from, and the field that chooses another. */
export function TierRow({ tier, catalogList }: TierRowProps) {
  const id = useId();
  const setModel = useSetTierModel(tier.tier);
  const removeModel = useRemoveTierModel(tier.tier);
  const [model, setModelText] = useState("");
  const source = tierSourceDisplay(tier.source);
  const problems = formProblems(setModel.error ?? removeModel.error, ["model"]);
  const pending = setModel.isPending || removeModel.isPending;

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (model.trim() === "" || pending) return;
    removeModel.reset();
    setModel.mutate(model, { onSuccess: () => setModelText("") });
  }

  function useFilesModel() {
    setModel.reset();
    removeModel.mutate();
  }

  return (
    <li className="grid gap-3 py-5 [&_code]:font-mono [&_code]:text-xs">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <h3 className="font-heading text-lg capitalize">{tier.tier}</h3>
          <p className="text-sm text-pretty text-muted-foreground">{tierPurpose(tier.tier)}</p>
        </div>
        <StatusPill tone={source.tone}>{source.label}</StatusPill>
      </div>

      <p aria-live="polite" className="text-sm break-all">
        <code className="text-sm">{tier.model}</code>
        {tier.source !== "file" && (
          <span className="text-muted-foreground">
            {" "}
            (the file has <code>{tier.file_model}</code>)
          </span>
        )}
      </p>

      {tier.source === "environment" ? (
        <p className="max-w-prose text-sm text-pretty text-muted-foreground">
          <code>{tier.environment_variable}</code> sets this tier in the environment of the API and the worker. To
          choose it here, unset the variable there and restart both.
        </p>
      ) : (
        <form onSubmit={save} className="grid max-w-prose gap-2" autoComplete="off" noValidate>
          <Label htmlFor={`${id}-model`}>
            Model<span className="sr-only"> of the {tier.tier} tier</span>
          </Label>
          <div className="flex flex-wrap gap-2">
            <Input
              id={`${id}-model`}
              list={catalogList}
              value={model}
              onChange={(event) => setModelText(event.target.value)}
              placeholder={tier.model}
              spellCheck={false}
              autoCapitalize="none"
              disabled={pending}
              className="min-w-48 flex-1 font-mono"
              aria-invalid={fieldProblems(problems, "model").length > 0}
              aria-describedby={`${id}-errors`}
            />
            <Button type="submit" disabled={pending || model.trim() === ""}>
              {setModel.isPending ? "Saving…" : "Choose"}
            </Button>
            {tier.chosen !== null && (
              <Button type="button" variant="outline" onClick={useFilesModel} disabled={pending}>
                Use the file&rsquo;s model
              </Button>
            )}
          </div>
          <FieldErrors id={`${id}-errors`} messages={fieldProblems(problems, "model")} />
        </form>
      )}

      {problems.form.length > 0 && (
        <Notice tone="danger" title="The tier was not changed" role="alert" className="max-w-prose">
          {problems.form.map((problem) => (
            <p key={problem}>
              <InlineCodeText text={problem} />
            </p>
          ))}
        </Notice>
      )}
    </li>
  );
}
