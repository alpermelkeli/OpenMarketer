import { LockIcon, PencilLineIcon } from "lucide-react";
import type { ReactNode } from "react";

import type { ProfileVersion } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

type ProfileStateBandProps = {
  version: ProfileVersion;
  /** The reviewer is changing a working copy of this version. */
  editing: boolean;
  /** The buttons that fit the state: edit and approve, or discard and save. */
  actions: ReactNode;
};

/**
 * Says, above the profile and while scrolling through it, whether what is on screen
 * is a draft or approved. The state is the server's (`version.status`); a draft is
 * dashed and amber, an approved version solid and green with a lock, and the word
 * is always written out.
 */
export function ProfileStateBand({ version, editing, actions }: ProfileStateBandProps) {
  const approved = version.status === "approved";
  const Icon = approved ? LockIcon : PencilLineIcon;

  return (
    <div
      role="status"
      className={cn(
        "sticky top-0 z-10 -mx-2 flex flex-wrap items-center justify-between gap-x-6 gap-y-3 rounded-md border bg-background px-4 py-3 sm:px-5",
        approved ? "border-success/60" : "border-dashed border-warning/70",
      )}
    >
      <div className="flex min-w-0 items-start gap-3">
        <span
          className={cn(
            "mt-0.5 inline-flex h-7 shrink-0 items-center gap-1.5 rounded-sm px-2.5 text-xs tracking-widest uppercase",
            approved ? "bg-success/12 text-success" : "bg-warning/12 text-warning",
          )}
        >
          <Icon aria-hidden="true" className="size-3.5" />
          {approved ? "Approved" : "Draft"}
        </span>
        <div className="min-w-0">
          <p className="font-heading text-lg">
            Version <span className="tabular-nums">{version.version}</span>
            {editing && <span className="text-muted-foreground"> · editing a copy</span>}
          </p>
          <p className="text-sm text-pretty text-muted-foreground">
            {editing
              ? `Saving stores your changes as a new draft. Version ${version.version} stays exactly as it is.`
              : approved
                ? `Approved ${version.approved_at === null ? "" : formatDateTime(version.approved_at)}. Locked: it can never be changed or deleted.`
                : `Created ${formatDateTime(version.created_at)}. Not approved: a proposal until a person approves it.`}
          </p>
        </div>
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>
    </div>
  );
}
