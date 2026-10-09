import { LockIcon, PencilLineIcon } from "lucide-react";
import Link from "next/link";

import type { ProfileStatus, ProfileVersionSummary } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

type LatestVersionsProps = {
  projectId: string;
  /** The project's latest draft and latest approved version numbers, as the API reports them. */
  latestDraft: number | null;
  latestApproved: number | null;
  /** The versions loaded so far, for the dates. */
  versions: readonly ProfileVersionSummary[];
  shownVersion: number;
  /** Leaving is held while there are unsaved edits. */
  locked: boolean;
};

/** The two versions that matter most, always one click away: the latest draft and the latest approved. */
export function LatestVersions({ projectId, latestDraft, latestApproved, versions, shownVersion, locked }: LatestVersionsProps) {
  const summaryOf = (version: number | null) => versions.find((summary) => summary.version === version) ?? null;
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <LatestVersion
        status="draft"
        projectId={projectId}
        version={latestDraft}
        summary={summaryOf(latestDraft)}
        shown={latestDraft === shownVersion}
        locked={locked}
        note={
          latestDraft !== null && latestApproved !== null && latestDraft < latestApproved
            ? `Older than the approved version ${latestApproved}.`
            : null
        }
      />
      <LatestVersion
        status="approved"
        projectId={projectId}
        version={latestApproved}
        summary={summaryOf(latestApproved)}
        shown={latestApproved === shownVersion}
        locked={locked}
        note={null}
      />
    </div>
  );
}

type LatestVersionProps = {
  status: ProfileStatus;
  projectId: string;
  version: number | null;
  summary: ProfileVersionSummary | null;
  shown: boolean;
  locked: boolean;
  note: string | null;
};

function LatestVersion({ status, projectId, version, summary, shown, locked, note }: LatestVersionProps) {
  const isDraft = status === "draft";
  const Icon = isDraft ? PencilLineIcon : LockIcon;
  const title = isDraft ? "Latest draft" : "Latest approved";
  const box = cn("block rounded-md border px-4 py-3 text-left text-sm", isDraft && "border-dashed");

  if (version === null) {
    return (
      <div className={cn(box, "text-muted-foreground")}>
        <p className="flex items-center gap-2">
          <Icon aria-hidden="true" className="size-3.5" />
          {title}
        </p>
        <p className="mt-1">{isDraft ? "No draft is waiting for review." : "Nothing has been approved yet."}</p>
      </div>
    );
  }

  const when =
    summary === null
      ? null
      : isDraft || summary.approved_at === null
        ? `created ${formatDateTime(summary.created_at)}`
        : `approved ${formatDateTime(summary.approved_at)}`;
  const content = (
    <>
      <span className="flex items-center justify-between gap-3">
        <span className={cn("flex items-center gap-2", isDraft ? "text-warning" : "text-success")}>
          <Icon aria-hidden="true" className="size-3.5" />
          {title}
        </span>
        <span className="text-xs text-muted-foreground">{shown ? "Shown below" : locked ? "" : "Show"}</span>
      </span>
      <span className="mt-1 block text-foreground">
        Version <span className="tabular-nums">{version}</span>
        {when !== null && <span className="text-muted-foreground"> · {when}</span>}
      </span>
      {note !== null && <span className="mt-0.5 block text-xs text-muted-foreground">{note}</span>}
    </>
  );

  if (shown || locked) {
    return (
      <div aria-current={shown ? "true" : undefined} className={cn(box, shown ? "border-foreground bg-card" : "opacity-60")}>
        {content}
      </div>
    );
  }
  return (
    <Link
      href={`/projects/${projectId}/profile?version=${version}`}
      scroll={false}
      className={cn(box, "transition-colors hover:border-border-strong hover:bg-card")}
    >
      {content}
    </Link>
  );
}
