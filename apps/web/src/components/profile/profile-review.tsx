"use client";

import { useEffect, useState } from "react";

import { Notice } from "@/components/common/notice";
import { Button } from "@/components/ui/button";
import { useApproveProfileVersion, useSaveProfileEdit } from "@/lib/api/profiles";
import type { ProductProfile, ProfileStatus, ProfileVersion } from "@/lib/api/types";
import { attentionItems } from "@/lib/profile/attention";
import { countChanges } from "@/lib/profile/edits";
import { approveProblem, saveEditProblems } from "@/lib/profile/review-problems";

import { ApproveDialog } from "./approve-dialog";
import { AttentionList } from "./attention-list";
import { ProfileDocument } from "./profile-document";
import { ProfileStateBand } from "./profile-state-band";
import { VersionSwitch } from "./version-switch";

type ProfileReviewProps = {
  projectId: string;
  repositoryUrl: string | null;
  draft: ProfileVersion | null;
  approved: ProfileVersion | null;
};

type WorkingCopy = { ofVersion: number; profile: ProductProfile };

/**
 * Reviewing a project's profile: choose the draft or the approved version, edit a
 * working copy into a new draft, and approve. Holds the screen's state and its two
 * mutations; what is drawn is in the components it composes.
 */
export function ProfileReview({ projectId, repositoryUrl, draft, approved }: ProfileReviewProps) {
  const saveEdit = useSaveProfileEdit(projectId);
  const approve = useApproveProfileVersion(projectId);
  const [chosen, setChosen] = useState<ProfileStatus | null>(null);
  const [workingCopy, setWorkingCopy] = useState<WorkingCopy | null>(null);
  const [approveOpen, setApproveOpen] = useState(false);

  // A draft newer than the approved version is what waits for the reviewer, so it is
  // shown first; an older draft is history, and the approved version comes first.
  const byStatus = { draft, approved };
  const draftIsNewest = draft !== null && (approved === null || draft.version > approved.version);
  const shown = (chosen !== null && byStatus[chosen]) || (draftIsNewest ? draft : approved) || draft;

  const editing = workingCopy !== null && shown !== null && workingCopy.ofVersion === shown.version;
  const changes = editing && shown !== null ? countChanges(shown.profile, workingCopy.profile) : 0;
  useWarningBeforeLeaving(changes > 0);

  if (shown === null) return null;

  const profile = editing ? workingCopy.profile : shown.profile;
  const flagged = attentionItems(profile);

  function stopEditing() {
    setWorkingCopy(null);
    saveEdit.reset();
  }

  function save() {
    if (workingCopy === null) return;
    saveEdit.mutate(
      { version: workingCopy.ofVersion, profile: workingCopy.profile },
      {
        onSuccess: () => {
          setWorkingCopy(null);
          setChosen("draft");
        },
      },
    );
  }

  function approveShown(version: number) {
    approve.mutate(version, {
      onSuccess: () => {
        setApproveOpen(false);
        setChosen("approved");
      },
    });
  }

  const actions = editing ? (
    <>
      <span className="text-sm text-muted-foreground tabular-nums" aria-live="polite">
        {changes === 0 ? "No changes" : changes === 1 ? "1 change" : `${changes} changes`}
      </span>
      <Button variant="ghost" size="lg" onClick={stopEditing} disabled={saveEdit.isPending}>
        Discard
      </Button>
      <Button size="lg" onClick={save} disabled={changes === 0 || saveEdit.isPending}>
        {saveEdit.isPending ? "Saving…" : "Save as new draft"}
      </Button>
    </>
  ) : (
    <>
      <Button
        variant="outline"
        size="lg"
        onClick={() => setWorkingCopy({ ofVersion: shown.version, profile: shown.profile })}
      >
        {shown.status === "approved" ? "Edit as new draft" : "Edit"}
      </Button>
      {shown.status === "draft" && (
        <Button
          size="lg"
          onClick={() => {
            approve.reset();
            setApproveOpen(true);
          }}
        >
          Approve version {shown.version}…
        </Button>
      )}
    </>
  );

  return (
    <div className="grid gap-8">
      <VersionSwitch
        draft={draft}
        approved={approved}
        shown={shown.status}
        onShow={setChosen}
        disabled={editing}
      />

      <ProfileStateBand version={shown} editing={editing} actions={actions} />

      {saveEdit.error !== null && (
        <Notice tone="danger" title="The edit was not saved" role="alert">
          <ul className="grid gap-1">
            {saveEditProblems(saveEdit.error).map((problem) => (
              <li key={problem} className="break-words">
                {problem}
              </li>
            ))}
          </ul>
        </Notice>
      )}

      <AttentionList items={flagged} />

      <p className="max-w-prose text-xs text-pretty text-muted-foreground">
        {repositoryUrl === null
          ? "Evidence is shown as file and lines. The dashboard does not know this project's repository, so it cannot link to the files."
          : "Evidence is a file and its lines. Where the repository is on a host the dashboard knows, a reference opens that file on the default branch as it is today; the analysed commit is not recorded here, so the lines may have moved since."}
      </p>

      <ProfileDocument
        profile={profile}
        repositoryUrl={repositoryUrl}
        editing={editing}
        onChange={(next) => setWorkingCopy({ ofVersion: shown.version, profile: next })}
      />

      <ApproveDialog
        open={approveOpen}
        onOpenChange={setApproveOpen}
        version={shown.version}
        flaggedCount={flagged.length}
        pending={approve.isPending}
        problem={approve.error === null ? null : approveProblem(approve.error)}
        onApprove={() => approveShown(shown.version)}
      />
    </div>
  );
}

/** Ask before the tab is closed or reloaded while edits are unsaved. */
function useWarningBeforeLeaving(unsaved: boolean): void {
  useEffect(() => {
    if (!unsaved) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [unsaved]);
}
