"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { Notice } from "@/components/common/notice";
import { Button } from "@/components/ui/button";
import { useApproveProfileVersion, useSaveProfileEdit } from "@/lib/api/profiles";
import type { ProductProfile, ProfileVersion, Project } from "@/lib/api/types";
import { attentionItems } from "@/lib/profile/attention";
import { changedParts, countChanges } from "@/lib/profile/edits";
import { approveProblem, saveEditProblems } from "@/lib/profile/review-problems";

import { ApproveDialog } from "./approve-dialog";
import { AttentionList } from "./attention-list";
import { ProfileDocument } from "./profile-document";
import { ProfileStateBand } from "./profile-state-band";
import { VersionProvenance } from "./version-provenance";

type ProfileReviewProps = {
  project: Project;
  /** The version on screen. */
  version: ProfileVersion;
  /** The version it was edited from, once loaded, to say what the edit changed. */
  parent: ProfileVersion | null;
  /** Told when editing starts and stops, so the screen can hold navigation to other versions. */
  onEditingChange: (editing: boolean) => void;
};

/**
 * Reviewing one version of a profile: read it, edit a working copy into a new draft,
 * and approve it. Holds the working copy and the two mutations; what is drawn is in
 * the components it composes. Mounted once per version, so its state never outlives
 * the version it belongs to.
 */
export function ProfileReview({ project, version, parent, onEditingChange }: ProfileReviewProps) {
  const router = useRouter();
  const saveEdit = useSaveProfileEdit(project.id);
  const approve = useApproveProfileVersion(project.id);
  const [workingCopy, setWorkingCopy] = useState<ProductProfile | null>(null);
  const [approveOpen, setApproveOpen] = useState(false);

  const editing = workingCopy !== null;
  const changes = editing ? countChanges(version.profile, workingCopy) : 0;
  useWarningBeforeLeaving(changes > 0);

  const profile = workingCopy ?? version.profile;
  const flagged = attentionItems(profile);
  const latestApproved = project.latest_approved_version;

  function setEditing(next: ProductProfile | null) {
    setWorkingCopy(next);
    onEditingChange(next !== null);
  }

  function stopEditing() {
    setEditing(null);
    saveEdit.reset();
  }

  function save() {
    if (workingCopy === null) return;
    saveEdit.mutate(
      { version: version.version, profile: workingCopy },
      {
        onSuccess: (newDraft) => {
          setEditing(null);
          router.push(`/projects/${project.id}/profile?version=${newDraft.version}`, { scroll: false });
        },
      },
    );
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
      <Button variant="outline" size="lg" onClick={() => setEditing(version.profile)}>
        {version.status === "approved" ? "Edit as new draft" : "Edit"}
      </Button>
      {version.status === "draft" && (
        <Button
          size="lg"
          onClick={() => {
            approve.reset();
            setApproveOpen(true);
          }}
        >
          Approve version {version.version}…
        </Button>
      )}
    </>
  );

  return (
    <div className="grid gap-8">
      <ProfileStateBand version={version} editing={editing} actions={actions} />

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

      <VersionProvenance
        version={version}
        repositoryUrl={project.repository_url}
        changesFromParent={parent === null ? null : changedParts(parent.profile, version.profile)}
      />

      <AttentionList items={flagged} />

      <ProfileDocument
        profile={profile}
        source={{ repositoryUrl: project.repository_url, commitSha: version.commit_sha }}
        editing={editing}
        onChange={setEditing}
      />

      <ApproveDialog
        open={approveOpen}
        onOpenChange={setApproveOpen}
        version={version.version}
        flaggedCount={flagged.length}
        newerApprovedVersion={latestApproved !== null && latestApproved > version.version ? latestApproved : null}
        pending={approve.isPending}
        problem={approve.error === null ? null : approveProblem(approve.error)}
        onApprove={() => approve.mutate(version.version, { onSuccess: () => setApproveOpen(false) })}
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
