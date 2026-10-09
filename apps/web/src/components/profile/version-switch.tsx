import { LockIcon, PencilLineIcon } from "lucide-react";

import type { ProfileStatus, ProfileVersion } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

type VersionSwitchProps = {
  draft: ProfileVersion | null;
  approved: ProfileVersion | null;
  shown: ProfileStatus;
  onShow: (status: ProfileStatus) => void;
  /** Switching is held while there are unsaved edits. */
  disabled: boolean;
};

/** The two versions the API can give: the latest draft and the latest approved one. */
export function VersionSwitch({ draft, approved, shown, onShow, disabled }: VersionSwitchProps) {
  return (
    <div role="group" aria-label="Version shown" className="grid gap-3 sm:grid-cols-2">
      <VersionOption
        status="draft"
        version={draft}
        current={shown === "draft"}
        disabled={disabled}
        onShow={onShow}
        note={
          draft !== null && approved !== null && draft.version < approved.version
            ? `Older than the approved version ${approved.version}.`
            : null
        }
      />
      <VersionOption status="approved" version={approved} current={shown === "approved"} disabled={disabled} onShow={onShow} note={null} />
    </div>
  );
}

type VersionOptionProps = {
  status: ProfileStatus;
  version: ProfileVersion | null;
  current: boolean;
  disabled: boolean;
  onShow: (status: ProfileStatus) => void;
  note: string | null;
};

function VersionOption({ status, version, current, disabled, onShow, note }: VersionOptionProps) {
  const isDraft = status === "draft";
  const Icon = isDraft ? PencilLineIcon : LockIcon;
  const title = isDraft ? "Latest draft" : "Latest approved";

  if (version === null) {
    return (
      <div className="rounded-md border border-dashed px-4 py-3 text-sm text-muted-foreground">
        <p className="flex items-center gap-2">
          <Icon aria-hidden="true" className="size-3.5" />
          {title}
        </p>
        <p className="mt-1">{isDraft ? "No draft is waiting for review." : "Nothing has been approved yet."}</p>
      </div>
    );
  }

  return (
    <button
      type="button"
      aria-pressed={current}
      disabled={disabled && !current}
      onClick={() => onShow(status)}
      className={cn(
        "rounded-md border px-4 py-3 text-left text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-60",
        isDraft && "border-dashed",
        current ? "border-foreground bg-card" : "hover:border-border-strong hover:bg-card",
      )}
    >
      <span className="flex items-center justify-between gap-3">
        <span className={cn("flex items-center gap-2", isDraft ? "text-warning" : "text-success")}>
          <Icon aria-hidden="true" className="size-3.5" />
          {title}
        </span>
        <span className="text-xs text-muted-foreground">{current ? "Shown below" : "Show"}</span>
      </span>
      <span className="mt-1 block text-foreground">
        Version <span className="tabular-nums">{version.version}</span>
        <span className="text-muted-foreground">
          {" · "}
          {isDraft || version.approved_at === null
            ? `created ${formatDateTime(version.created_at)}`
            : `approved ${formatDateTime(version.approved_at)}`}
        </span>
      </span>
      {note !== null && <span className="mt-0.5 block text-xs text-muted-foreground">{note}</span>}
    </button>
  );
}
