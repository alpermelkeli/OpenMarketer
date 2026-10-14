"use client";

import { useId } from "react";

import { Notice } from "@/components/common/notice";
import { RequestProblem } from "@/components/common/request-problem";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  useModelCheck,
  useModelSettings,
  useRemoveRoleChoice,
  useRemoveTierModel,
  useSetRejectedParameters,
} from "@/lib/api/model-settings";
import type { ModelProblem, RoleSetting, UnreadableChoice } from "@/lib/api/types";
import { roleLabel } from "@/lib/settings/model-labels";

import { ModelCatalogList, ModelCatalogStatus } from "./model-catalog-list";
import { RejectedParameters } from "./rejected-parameters";
import { RoleRow } from "./role-row";
import { TierRow } from "./tier-row";

const NO_PROBLEMS: readonly ModelProblem[] = [];

/**
 * The models section: the tiers, the roles and the models that reject the temperature,
 * each with what it resolves to now and the way to choose otherwise.
 */
export function ModelsScreen() {
  const catalogList = useId();
  const settings = useModelSettings();
  const check = useModelCheck();
  const data = settings.data;
  const problemsOf = (role: string) => check.data?.problems.filter((problem) => problem.role === role) ?? NO_PROBLEMS;
  const inUse = data?.roles.filter((role) => roleLabel(role.role).inUse) ?? [];
  const waiting = data?.roles.filter((role) => !roleLabel(role.role).inUse) ?? [];
  const rowOf = (role: RoleSetting) =>
    data === undefined ? null : (
      <RoleRow
        key={role.role}
        role={role}
        problems={problemsOf(role.role)}
        bounds={data.bounds}
        catalogList={catalogList}
      />
    );

  return (
    <section aria-labelledby="models" className="grid gap-8 [&_code]:font-mono [&_code]:text-xs">
      <div className="grid gap-2">
        <h2 id="models" className="font-heading text-title">
          Models
        </h2>
        <p className="max-w-prose text-pretty text-muted-foreground">
          Which model each part of OpenMarketer calls, through OpenRouter. A choice made here is used from a
          run&rsquo;s next step on, in place of the configuration file&rsquo;s (<code>config/models.yaml</code>).
          An environment variable still wins over both.
        </p>
        <ModelCatalogStatus />
      </div>
      <ModelCatalogList id={catalogList} />

      {settings.error !== null && (
        <RequestProblem
          title="The model settings could not be read"
          error={settings.error}
          onRetry={() => void settings.refetch()}
        />
      )}

      {data === undefined && settings.error === null && (
        <div className="grid gap-3" aria-busy="true" aria-label="Loading the model settings">
          <Skeleton className="h-28" />
          <Skeleton className="h-28" />
        </div>
      )}

      {data !== undefined && data.unreadable.length > 0 && <UnreadableChoices choices={data.unreadable} />}

      {data !== undefined && (
        <>
          <section aria-labelledby={`${catalogList}-tiers`} className="grid gap-2">
            <h3 id={`${catalogList}-tiers`} className="font-heading text-lg">
              Tiers
            </h3>
            <p className="max-w-prose text-sm text-pretty text-muted-foreground">
              A role that names no model of its own follows its tier, so changing a tier moves every role on it.
            </p>
            <ul className="divide-y border-y">
              {data.tiers.map((tier) => (
                <TierRow key={tier.tier} tier={tier} catalogList={catalogList} />
              ))}
            </ul>
          </section>

          <section aria-labelledby={`${catalogList}-roles`} className="grid gap-2">
            <h3 id={`${catalogList}-roles`} className="font-heading text-lg">
              Roles
            </h3>
            <p className="max-w-prose text-sm text-pretty text-muted-foreground">
              Only the repository analyzer runs today; the other roles belong to agents still to be built, and what is
              chosen for them waits until then.
            </p>
            <ul className="divide-y border-y">{inUse.map(rowOf)}</ul>
            {waiting.length > 0 && (
              <details className="group">
                <summary className="cursor-pointer py-3 text-sm text-muted-foreground hover:text-foreground">
                  Roles of agents still to be built ({waiting.length})
                </summary>
                <ul className="divide-y border-y">{waiting.map(rowOf)}</ul>
              </details>
            )}
          </section>

          <RejectedParameters rejected={data.rejected_parameters} catalogList={catalogList} />
        </>
      )}
    </section>
  );
}

type UnreadableChoicesProps = { choices: readonly UnreadableChoice[] };

/** Stored choices that break a rule: runs are refused until each is reset. */
function UnreadableChoices({ choices }: UnreadableChoicesProps) {
  return (
    <Notice tone="danger" title="Some stored choices are not valid any more" role="alert" className="max-w-prose">
      <p>Analyses fail until each is reset. Resetting goes back to the configuration file.</p>
      <ul className="mt-2 grid gap-2">
        {choices.map((choice) => (
          <li key={`${choice.kind} ${choice.key}`} className="flex flex-wrap items-center justify-between gap-2">
            <span>
              {choice.kind === "tier" ? "The tier" : choice.kind === "role" ? "The role" : "The rejected parameters of"}{" "}
              <code>{choice.key}</code>
            </span>
            <ResetChoice choice={choice} />
          </li>
        ))}
      </ul>
    </Notice>
  );
}

function ResetChoice({ choice }: { choice: UnreadableChoice }) {
  const removeTier = useRemoveTierModel(choice.key);
  const removeRole = useRemoveRoleChoice(choice.key);
  const setRejected = useSetRejectedParameters();
  const pending = removeTier.isPending || removeRole.isPending || setRejected.isPending;

  function reset() {
    if (choice.kind === "tier") removeTier.mutate();
    else if (choice.kind === "role") removeRole.mutate();
    else setRejected.mutate({ model: choice.key, parameters: [] });
  }

  return (
    <Button type="button" size="sm" variant="outline" onClick={reset} disabled={pending}>
      Reset<span className="sr-only"> {choice.key}</span>
    </Button>
  );
}
