import { ArrowUpRightIcon } from "lucide-react";
import Link from "next/link";

import { FieldErrors } from "@/components/common/field-errors";
import { LoadMore } from "@/components/common/load-more";
import type { ResearchRun } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";
import { projectPageHref, researchHref } from "@/lib/projects/project-navigation";
import type { Offered } from "@/lib/writing/brief-form";

type PostResearchChoiceProps = {
  id: string;
  projectId: string;
  runs: Offered<ResearchRun>;
  /** The chosen run's id; "" for none. */
  value: string;
  onChange: (runId: string) => void;
  problems: readonly string[];
  disabled: boolean;
  more: { hasMore: boolean; loading: boolean; onLoadMore: () => void };
};

const CHOICE =
  "flex min-w-0 flex-1 cursor-pointer flex-wrap items-baseline gap-x-2 rounded-sm border px-3 py-2 text-sm transition-colors peer-checked:border-brand peer-checked:bg-brand/5 peer-focus-visible:ring-3 peer-focus-visible:ring-ring/50 peer-disabled:cursor-not-allowed peer-disabled:opacity-60";

/**
 * Which research report the writer may draw on, if any: one choice per run that succeeded, each
 * with a link to its report, after "No research". A list of runs does not carry their questions,
 * so a run is named by when it finished; its report says what it asked.
 */
export function PostResearchChoice({
  id,
  projectId,
  runs,
  value,
  onChange,
  problems,
  disabled,
  more,
}: PostResearchChoiceProps) {
  const name = `${id}-research`;
  return (
    <fieldset className="grid gap-2" aria-describedby={`${id}-research-hint ${id}-research-errors`}>
      <legend className="mb-2 text-sm">Research the post may draw on</legend>
      <ul className="grid gap-2">
        <li className="flex">
          <input
            type="radio"
            id={`${name}-none`}
            name={name}
            value=""
            checked={value === ""}
            onChange={() => onChange("")}
            disabled={disabled}
            className="peer sr-only"
          />
          <label htmlFor={`${name}-none`} className={CHOICE}>
            No research: the post draws on the approved profile alone
          </label>
        </li>
        {(runs.state === "known" ? runs.items : []).map((run) => (
          <li key={run.id} className="flex items-center gap-3">
            <input
              type="radio"
              id={`${name}-${run.id}`}
              name={name}
              value={run.id}
              checked={value === run.id}
              onChange={() => onChange(run.id)}
              disabled={disabled}
              className="peer sr-only"
            />
            <label htmlFor={`${name}-${run.id}`} className={CHOICE}>
              <span>Research finished {formatDateTime(run.finished_at ?? run.created_at)}</span>
              <span className="font-mono text-xs text-muted-foreground">{shortId(run.id)}</span>
            </label>
            <Link
              href={researchHref(projectId, run.id)}
              target="_blank"
              className="inline-flex shrink-0 items-center gap-1 rounded-sm text-sm text-brand underline-offset-4 hover:underline"
            >
              Its report
              <ArrowUpRightIcon aria-hidden="true" className="size-3.5" />
              <span className="sr-only">(research {shortId(run.id)}, opens in a new tab)</span>
            </Link>
          </li>
        ))}
      </ul>
      {runs.state === "reading" && (
        <p className="text-xs text-muted-foreground">Reading the project&rsquo;s research…</p>
      )}
      <LoadMore noun="research runs" hasMore={more.hasMore} loading={more.loading} onLoadMore={more.onLoadMore} />
      <p id={`${id}-research-hint`} className="text-xs text-pretty text-muted-foreground">
        {runs.state === "known" && runs.items.length === 0 && !more.hasMore ? (
          <>
            No{" "}
            <Link
              href={projectPageHref(projectId, "research")}
              className="rounded-sm text-brand underline-offset-4 hover:underline"
            >
              research
            </Link>{" "}
            of this project has succeeded yet.
          </>
        ) : (
          "Only research that succeeded is offered. Its findings may shape the post's angle and be quoted as what the web says; a claim about the product still rests on a live feature of the profile."
        )}
      </p>
      <FieldErrors id={`${id}-research-errors`} messages={problems} />
    </fieldset>
  );
}
