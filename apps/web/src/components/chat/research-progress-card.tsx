import { ArrowRightIcon } from "lucide-react";
import Link from "next/link";

import { Notice } from "@/components/common/notice";
import { StatusPill } from "@/components/common/status-pill";
import type { ResearchRunView } from "@/lib/chat/events";
import { type ProgressStatus, outcomeText, progressDisplay, stageText } from "@/lib/chat/research-progress";
import { shortId } from "@/lib/format";
import { researchHref } from "@/lib/projects/project-navigation";

type ResearchProgressCardProps = {
  projectId: string;
  view: ResearchRunView;
  status: ProgressStatus;
};

/**
 * A research run a turn of the chat started, as it goes: each stage it recorded, its state, and
 * the way to its own screen. Queries, titles and addresses are what a model or a web page wrote,
 * shown as text; the run's screen is where its sources are linked.
 */
export function ResearchProgressCard({ projectId, view, status }: ResearchProgressCardProps) {
  const display = progressDisplay(status.state);
  const outcome = outcomeText(status);
  return (
    <article
      aria-label={`Research ${shortId(view.id)}`}
      className="grid w-full max-w-[min(42rem,100%)] gap-3 justify-self-start rounded-md border bg-card px-4 py-3 text-sm"
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <p className="text-2xs tracking-widest text-muted-foreground uppercase">Research</p>
        <StatusPill tone={display.tone} live={status.state === "running"}>
          {display.label}
        </StatusPill>
        <Link
          href={researchHref(projectId, view.id)}
          className="ml-auto inline-flex items-center gap-1 rounded-sm text-brand underline-offset-4 hover:underline"
        >
          {status.state === "finished" ? "Read the report" : "Open the run"}
          <ArrowRightIcon aria-hidden="true" className="size-3.5" />
        </Link>
      </header>

      {/* Polite, so a screen reader hears the run move on without being interrupted. */}
      <div aria-live="polite" className="grid gap-2">
        {view.stages.length === 0 && status.state === "running" && (
          <p className="text-muted-foreground">Starting: the run plans its queries first.</p>
        )}
        {view.stages.length > 0 && (
          <ol className="grid gap-1.5 text-muted-foreground">
            {view.stages.map((stage, index) => (
              <li key={index} className="min-w-0 break-words">
                {stageText(stage)}
                {stage.kind === "planned" && stage.queries.length > 0 && (
                  <ul className="mt-1 grid gap-0.5 border-l pl-3 text-xs">
                    {stage.queries.map((query, queryIndex) => (
                      <li key={queryIndex}>{query}</li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ol>
        )}
        {outcome !== null && <p>{outcome}</p>}
        {status.state === "failed" && (
          <Notice tone="danger" title="The research failed" role="status">
            <p className="break-words whitespace-pre-wrap text-foreground">{status.reason ?? "No reason was recorded."}</p>
          </Notice>
        )}
      </div>
    </article>
  );
}
