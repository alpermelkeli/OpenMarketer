"use client";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

type ApproveDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  version: number;
  /** How many parts are still listed under "Needs your attention". */
  flaggedCount: number;
  pending: boolean;
  /** Why the last attempt failed, in words for the reviewer. */
  problem: string | null;
  onApprove: () => void;
};

/** The one deliberate step that turns a draft into the approved profile. It says what cannot be undone. */
export function ApproveDialog({
  open,
  onOpenChange,
  version,
  flaggedCount,
  pending,
  problem,
  onApprove,
}: ApproveDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="data-[size=default]:sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="font-heading text-title">Approve version {version}?</AlertDialogTitle>
          <AlertDialogDescription>
            Approval is permanent. An approved version can never be changed or deleted, by you, by this dashboard or
            by the agent. To correct something later you edit it into a new draft and approve that one.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {flaggedCount > 0 && (
          <p className="rounded-md border border-warning/45 bg-warning/8 px-3 py-2 text-sm text-pretty">
            {flaggedCount === 1 ? "1 part is" : `${flaggedCount} parts are`} still listed under &ldquo;Needs your
            attention&rdquo;. Approving accepts them as they are.
          </p>
        )}
        {problem !== null && (
          <p role="alert" className="text-sm text-pretty text-destructive">
            {problem}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <AlertDialogAction onClick={onApprove} disabled={pending}>
            {pending ? "Approving…" : `Approve version ${version} permanently`}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
