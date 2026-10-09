import Link from "next/link";

import { LoadMore } from "@/components/common/load-more";
import { StatusPill } from "@/components/common/status-pill";
import type { ProfileVersionSummary } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { shortCommit } from "@/lib/profile/version-labels";
import { cn } from "@/lib/utils";

type VersionHistoryProps = {
  projectId: string;
  versions: readonly ProfileVersionSummary[];
  shownVersion: number;
  /** Open at first sight, for a reader who arrived at a version that is not one of the latest two. */
  defaultOpen: boolean;
  /** Leaving is held while there are unsaved edits. */
  locked: boolean;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
};

/** Every version of the profile, newest first: what each is, where it came from, and a way to open it. */
export function VersionHistory({
  projectId,
  versions,
  shownVersion,
  defaultOpen,
  locked,
  hasMore,
  loadingMore,
  onLoadMore,
}: VersionHistoryProps) {
  return (
    <details open={defaultOpen || undefined} className="group rounded-md border">
      <summary className="flex cursor-pointer items-center justify-between gap-4 rounded-md px-4 py-2.5 text-sm select-none hover:bg-surface">
        <span>
          Version history
          <span className="ml-2 text-muted-foreground tabular-nums">
            {versions.length}
            {hasMore ? "+" : ""} {versions.length === 1 && !hasMore ? "version" : "versions"}
          </span>
        </span>
        <span aria-hidden="true" className="text-xs text-muted-foreground group-open:hidden">
          Show
        </span>
        <span aria-hidden="true" className="hidden text-xs text-muted-foreground group-open:inline">
          Hide
        </span>
      </summary>
      <ol className="divide-y border-t">
        {versions.map((version) => {
          const shown = version.version === shownVersion;
          const approved = version.status === "approved";
          const label = `Version ${version.version}`;
          return (
            <li
              key={version.version}
              aria-current={shown ? "true" : undefined}
              className={cn("flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2.5 text-sm", shown && "bg-surface")}
            >
              <span className="w-24 shrink-0">
                {shown || locked ? (
                  <span className={cn(!shown && "text-muted-foreground")}>{label}</span>
                ) : (
                  <Link
                    href={`/projects/${projectId}/profile?version=${version.version}`}
                    scroll={false}
                    className="rounded-sm text-brand underline-offset-4 hover:underline"
                  >
                    {label}
                  </Link>
                )}
              </span>
              <StatusPill tone={approved ? "success" : "warning"} className={cn(!approved && "border-dashed")}>
                {approved ? "Approved" : "Draft"}
              </StatusPill>
              <span className="min-w-0 flex-1 basis-64 text-xs text-pretty text-muted-foreground">
                Created {formatDateTime(version.created_at)}
                {version.approved_at !== null && ` · approved ${formatDateTime(version.approved_at)}`}
                {" · "}
                {version.edited_from_version === null
                  ? "drafted by an analysis"
                  : `edited from version ${version.edited_from_version}`}
                {version.commit_sha !== null && (
                  <>
                    {" · commit "}
                    <span className="font-mono">{shortCommit(version.commit_sha)}</span>
                  </>
                )}
              </span>
              {shown && <span className="text-xs text-muted-foreground">Shown below</span>}
            </li>
          );
        })}
      </ol>
      <div className="px-4 pb-3 empty:hidden">
        <LoadMore noun="versions" hasMore={hasMore} loading={loadingMore} onLoadMore={onLoadMore} />
      </div>
    </details>
  );
}
