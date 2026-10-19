import { ArrowRightIcon } from "lucide-react";
import Link from "next/link";

import { Notice } from "@/components/common/notice";
import { Spinner } from "@/components/common/spinner";
import { StatusPill } from "@/components/common/status-pill";
import { VersionImage } from "@/components/studio/version-image";
import type { StudioRunView } from "@/lib/chat/events";
import {
  type Candidate,
  type StudioProgressStatus,
  studioOutcomeText,
  studioStageText,
} from "@/lib/chat/studio-progress";
import { shortId } from "@/lib/format";
import { studioHref } from "@/lib/projects/project-navigation";
import { isStudioRunFinished, studioRunStatusDisplay } from "@/lib/studio/status";

import { RunProposalDecision } from "./run-proposal-decision";

type StudioProgressCardProps = {
  projectId: string;
  view: StudioRunView;
  status: StudioProgressStatus;
  candidate: Candidate | null;
  /** What the image is, for someone who cannot see it. */
  alt: string;
};

/**
 * A studio run a turn of the chat started, as it goes: each step it recorded, its state, the image
 * it ended with, and the way to its own page, where its rounds and the decision on its image are.
 * The steps are open while the run is going and folded once it has ended, and the person can fold
 * them either way: folded, the line shows the step the run is on, turning, or how many it recorded.
 * A spend the policy gate held for a person is shown with the buttons to decide it.
 */
export function StudioProgressCard({ projectId, view, status, candidate, alt }: StudioProgressCardProps) {
  const display = studioRunStatusDisplay(status.state);
  const ended = isStudioRunFinished({ status: status.state });
  const outcome = studioOutcomeText(status);
  const lastStage = view.stages.at(-1) ?? null;
  const stepsLabel = `Steps (${view.stages.length})`;
  return (
    <article
      aria-label={`Studio run ${shortId(view.id)}`}
      className="grid w-full max-w-[min(42rem,100%)] gap-3 justify-self-start rounded-md border bg-card px-4 py-3 text-sm"
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <p className="text-2xs tracking-widest text-muted-foreground uppercase">Studio</p>
        <StatusPill tone={display.tone} live={!ended}>
          {display.label}
        </StatusPill>
        <Link
          href={studioHref(projectId, view.id)}
          className="ml-auto inline-flex items-center gap-1 rounded-sm text-brand underline-offset-4 hover:underline"
        >
          {ended && status.state !== "failed" ? "Review the image" : "Open the run"}
          <ArrowRightIcon aria-hidden="true" className="size-3.5" />
        </Link>
      </header>

      {view.proposal !== null && <RunProposalDecision projectId={projectId} proposal={view.proposal} />}

      {/* Polite, so a screen reader hears the run move on without being interrupted. */}
      <div aria-live="polite" className="grid gap-2">
        {view.stages.length === 0 && !ended && (
          <p className="text-muted-foreground">
            {status.state === "queued"
              ? "Waiting for a worker to pick it up."
              : "Starting: the studio plans the image first."}
          </p>
        )}
        {view.stages.length > 0 && (
          <details open={!ended || undefined} className="group">
            <summary className="w-fit cursor-pointer rounded-sm text-muted-foreground hover:text-foreground">
              <span className="inline-flex min-w-0 items-center gap-1.5 group-open:hidden">
                {!ended && lastStage !== null && (
                  <>
                    <Spinner />
                    <span className="sr-only">Working: </span>
                  </>
                )}
                <span className="min-w-0 break-words">
                  {!ended && lastStage !== null ? studioStageText(lastStage) : stepsLabel}
                </span>
              </span>
              <span className="hidden group-open:inline">{stepsLabel}</span>
            </summary>
            <ol className="mt-2 grid gap-1.5 text-muted-foreground">
              {view.stages.map((stage, index) => (
                <li key={index} className="min-w-0 break-words">
                  {studioStageText(stage)}
                </li>
              ))}
            </ol>
          </details>
        )}
        {candidate !== null && (
          <figure className="grid w-40 gap-1">
            <div className="overflow-hidden rounded-sm border">
              <VersionImage
                projectId={projectId}
                assetId={candidate.assetId}
                versionId={candidate.versionId}
                alt={alt}
                sizes="10rem"
              />
            </div>
            <figcaption className="text-xs text-muted-foreground">
              {candidate.final ? "The image it ended with" : "Its newest image"}
            </figcaption>
          </figure>
        )}
        {outcome !== null && <p>{outcome}</p>}
        {status.state === "failed" && (
          <Notice tone="danger" title="The studio run failed" role="status">
            <p className="break-words whitespace-pre-wrap text-foreground">{status.reason ?? "No reason was recorded."}</p>
            <p className="mt-2">It ended without an image for you to review.</p>
          </Notice>
        )}
      </div>
    </article>
  );
}
